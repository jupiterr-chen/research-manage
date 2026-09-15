"""LOCAL 自动化验收(ACCEPTANCE.md §1;`pytest -m acceptance`)。

AM-01/02/03abc/04/05/07/08/09/10/11/16/17/18;AM-06/12/13/14/15 见
test_acceptance_static.py;AM-03d 见 test_am03_real.py(-m real)。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from app.executor.status_reader import read_status
from app.scheduler import Scheduler
from app.services import runs as runs_srv
from app.services import schedules as sch_srv
from tests.acceptance.harness import (
    AcceptanceEnv,
    exec_containers,
    requires_sim,
    wait_agents_done,
    wait_status,
)

pytestmark = [pytest.mark.acceptance, requires_sim]


@pytest.fixture
def env(tmp_path, make_settings):
    e = AcceptanceEnv(tmp_path, make_settings)
    yield e
    for cid in exec_containers():
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)


def start(env, **overrides):
    w = env.worker()
    for k, v in overrides.items():
        setattr(env.settings, k, v)
    w.start()
    return w


def stop(w):
    w.stop()
    w.join(timeout=20)


class TestAM01Busy:
    def test_busy_409_zero_side_effect_and_inline_page(self, env):
        """AM-01:慢任务运行中,API 409 busy + 零副作用;页面内联。"""
        env.set_sim("slow", step="0.6")
        w = start(env)
        c = env.client(w)
        rid_a = None
        try:
            r1 = c.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1})
            assert r1.status_code == 200
            rid_a = r1.json()["id"]
            deadline = time.time() + 30
            while time.time() < deadline and wait_status(env, rid_a, terminal=False)["status"] != "running":
                time.sleep(0.4)

            r2 = c.post("/api/v1/runs", json={"code": "NVDA", "profile_id": 1})
            assert r2.status_code == 409
            body = r2.json()
            assert body["error"] == "busy" and body["current"]["id"] == rid_a
            assert body["current"]["code"] == "1810.HK" and body["queued"] == 0
            # 零副作用:无第二个执行容器、无 B 的 run 行与审计
            assert len(exec_containers()) == 1
            conn = env.conn()
            try:
                assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1
                assert (
                    conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='create_run'").fetchone()[
                        "n"
                    ]
                    == 1
                )
            finally:
                conn.close()
            # 页面:内联提示当前任务,不跳转
            page = c.post("/fragments/run", data={"code": "NVDA", "mode": "profile", "profile_id": "1"})
            assert page.status_code == 200 and "当前忙" in page.text
            assert "1810.HK" in page.text
        finally:
            if rid_a:
                conn = env.conn()
                try:
                    runs_srv.request_cancel(conn, rid_a, actor="web")
                except Exception:
                    pass
                finally:
                    conn.close()
            stop(w)


class TestAM02Fifo:
    def test_three_schedules_fifo(self, env):
        """AM-02:同刻三条调度按 id 顺序入队、串行执行、全部成功。"""
        env.set_sim("ok", step="0.4")
        conn = env.conn()
        try:
            for iid in (1, 2, 3):
                sch_srv.create(conn, instrument_id=iid, profile_id=1, kind="daily_trading", actor="web")
        finally:
            conn.close()
        from app.db import connect as _connect

        sched = Scheduler(env.settings, db_factory=lambda: _connect(env.db_path))
        w = start(env)
        try:
            for i in (1, 2, 3):  # 测试钩子:同 tick 按 schedule.id 顺序触发
                sched._fire(i)
                time.sleep(1.1)  # created_at 秒级可区分
            # 等全部终态
            deadline = time.time() + 180
            while time.time() < deadline:
                conn = env.conn()
                try:
                    rows = conn.execute(
                        "SELECT id, status, created_at, started_at FROM run ORDER BY created_at"
                    ).fetchall()
                    if len(rows) == 3 and all(r["status"] == "succeeded" for r in rows):
                        break
                finally:
                    conn.close()
                time.sleep(1)
            conn = env.conn()
            try:
                rows = conn.execute(
                    "SELECT id, status, created_at, started_at, trigger FROM run ORDER BY created_at"
                ).fetchall()
                assert len(rows) == 3 and all(r["status"] == "succeeded" for r in rows)
                assert [r["created_at"] for r in rows] == sorted(r["created_at"] for r in rows)
                assert [r["started_at"] for r in rows] == sorted(
                    r["started_at"] for r in rows
                )  # FIFO 启动顺序
                assert all(r["trigger"] == "schedule" for r in rows)
                assert (
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM audit_log WHERE actor='schedule' AND action='create_run'"
                    ).fetchone()["n"]
                    == 3
                )
            finally:
                conn.close()
        finally:
            stop(w)


class TestAM03DoubleVerdict:
    def test_a_no_report_failed(self, env):
        env.set_sim("no_report")
        w = start(env)
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            row = wait_status(env, run["id"])
            assert row["status"] == "failed"
            assert "investment_plan.md" in row["error"]
            assert row["exit_code"] == 0 and row["report_ready"] is False
        finally:
            stop(w)

    def test_b_no_memory_failed(self, env):
        env.set_sim("no_memory")
        w = start(env)
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            row = wait_status(env, run["id"])
            assert row["status"] == "failed"
            assert "trading_memory" in row["error"]
            assert row["report_ready"] is True  # plan 存在,实况为准
        finally:
            stop(w)

    def test_c_ok_three_artifacts(self, env):
        env.set_sim("ok")
        w = start(env)
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            row = wait_status(env, run["id"])
            assert row["status"] == "succeeded" and row["report_ready"] is True
            ta = Path(env.settings.ta_data_dir)
            date = row["analysis_date"]
            assert (ta / "logs" / "1810.HK" / date / "reports" / "investment_plan.md").is_file()
            memory = (ta / "memory" / "trading_memory.md").read_text(encoding="utf-8")
            assert any(line.startswith(f"[{date} | 1810.HK |") for line in memory.splitlines())
            log = json.loads(
                (
                    ta / "logs" / "1810.HK" / "TradingAgentsStrategy_logs" / f"full_states_log_{date}.json"
                ).read_text(encoding="utf-8")
            )
            assert log["final_trade_decision"]
        finally:
            stop(w)


class TestAM04CancelResume:
    def test_cancel_sigint_and_resume_skips_nodes(self, env):
        env.set_sim("slow", step="0.8")
        w = start(env)
        c = env.client(w)
        try:
            rid = c.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1}).json()["id"]
            done_at_cancel = wait_agents_done(env, rid, minimum=2, timeout=120)
            r = c.post(f"/api/v1/runs/{rid}/cancel")
            assert r.status_code == 200
            row = wait_status(env, rid, timeout=60)
            assert row["status"] == "cancelled" and row["exit_code"] == 130
            cps = list((Path(env.settings.ta_data_dir) / "cache" / "checkpoints").glob("*.json"))
            assert cps, "断点保留"
            assert row["analysts_csv"] == "market,social,news"

            r2 = c.post(f"/api/v1/runs/{rid}/resume")
            assert r2.status_code == 200
            new = r2.json()
            assert new["resumed_from"] == rid and new["analysts"] == ["market", "social", "news"]
            row2 = wait_status(env, new["id"], timeout=120)
            assert row2["status"] == "succeeded"
            tokens = row2["tokens_in"]
            # 跳过已完成节点:token 少于全量,且为单节点用量(120)的整数倍;
            # (DB 观测的 done_at_cancel 因轮询滞后小于实际断点数,只作下界参考)
            assert tokens < 11 * 120, "续跑 token 少于全量 → 跳过已完成节点"
            assert tokens > 0 and tokens % 120 == 0
            assert tokens <= (11 - max(done_at_cancel - 2, 1)) * 120
            # ⑤ resume 不接受改动 analysts
            r3 = c.post(f"/api/v1/runs/{rid}/resume", json={"analysts": ["market", "news"]})
            assert r3.status_code == 400  # 多余字段拒绝
        finally:
            stop(w)


class TestAM05Watchdog:
    def test_watchdog_timeout(self, env):
        env.set_sim("hang")
        w = start(env, watchdog_minutes=0.05)  # 3s
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            row = wait_status(env, run["id"], timeout=120)
            assert row["status"] == "cancelled"
            assert row["error"] == "watchdog_timeout"
            assert exec_containers(run["id"]) == []
            # 偏差说明:worker 内部转移不属于 SPEC 约束8 的 actor 枚举,无 run.watchdog 审计
        finally:
            stop(w)


class TestAM07Secrets:
    def _gather_targets(self, env, client, rid) -> list[str]:
        blobs = [
            client.get("/").text,
            client.get("/instruments").text,
            client.get(f"/runs/{rid}").text,
            client.get(f"/api/v1/runs/{rid}").text,
            client.get(f"/api/v1/runs/{rid}/artifacts").text,
        ]
        conn = env.conn()
        try:
            blobs += [r["detail_json"] or "" for r in conn.execute("SELECT detail_json FROM audit_log")]
        finally:
            conn.close()
        for f in (Path(env.settings.data_dir) / "runs").rglob("container.log"):
            blobs.append(f.read_text(encoding="utf-8"))
        return blobs

    def test_no_secret_leak_and_env_whitelist(self, env):
        """AM-07:ta.env 假密钥不出现在页面/API/审计/container.log;执行容器 env 白名单。"""
        env.set_sim("hang")  # hang 便于 inspect 执行容器
        w = start(env)
        c = env.client(w)
        try:
            rid = c.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1}).json()["id"]
            wait_status(env, rid, terminal=False)
            time.sleep(3)
            # docker inspect:Config.Env 白名单
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{json .Config.Env}}", exec_containers(rid)[0]],
                capture_output=True,
                text=True,
                timeout=30,
            )
            envs = json.loads(r.stdout)
            # 白名单:AM_RUN_ID/TZ/SIM_*/TRADINGAGENTS_* + python 基础镜像自带 env;
            # 其余任何键(尤其 .env 来源)都不得出现
            allowed = re.compile(
                r"^(AM_RUN_ID|TZ|SIM_[A-Z_]+|TRADINGAGENTS_[A-Z_]+|PATH|LANG|HOME|"
                r"HOSTNAME|GPG_KEY|PYTHON[A-Z0-9_]*)="
            )
            unexpected = [e for e in envs if not allowed.match(e)]
            assert unexpected == [], unexpected
            assert not any("FAKEKEY" in e for e in envs)
            c.post(f"/api/v1/runs/{rid}/cancel")
            wait_status(env, rid, timeout=60)
            # ok + fail 各一次,再全量扫描
            for mode, code in (("ok", "NVDA"), ("fail", "600519.SS")):
                env.set_sim(mode)
                resp = c.post("/api/v1/runs", json={"code": code, "profile_id": 1})
                assert resp.status_code == 200, resp.text
                rid2 = resp.json()["id"]
                wait_status(env, rid2, timeout=120)
            for blob in self._gather_targets(env, c, rid2):
                assert "FAKEKEY" not in blob
            # 代码路径扫描:app/ 不读取 .env(launcher 只出现挂载路径字符串)
            hits = []
            for py in (Path(env.settings.data_dir).parents[3] / "app").rglob("*.py"):
                text = py.read_text(encoding="utf-8")
                for pat in ("load_dotenv", "dotenv", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY"):
                    if pat in text:
                        hits.append(f"{py.name}:{pat}")
            assert hits == [], hits
        finally:
            stop(w)


class TestAM08CorruptStatus:
    def test_corrupt_keeps_last_value_and_stale(self, env):
        env.set_sim("corrupt_status")
        w = start(env, status_poll_seconds=1)
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            saw_stale, saw_value_kept = False, False
            last_done = -1
            deadline = time.time() + 60
            while time.time() < deadline:
                row = wait_status(env, run["id"], terminal=False)
                if row["status_stale"]:
                    saw_stale = True
                    if row["agents_done"] is not None and row["agents_done"] >= last_done:
                        saw_value_kept = True  # 保持上次值(COALESCE)
                last_done = max(last_done, row["agents_done"] or 0)
                if row["status"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.4)
            row = wait_status(env, run["id"], timeout=60)
            assert saw_stale, "损坏窗口内 status_stale=1"
            assert saw_value_kept
            assert row["status"] == "succeeded"
            conn = env.conn()
            try:
                final = runs_srv.get(conn, run["id"])
                assert final["status_stale"] in (0, 1)  # 恢复后由最后一次同步决定
            finally:
                conn.close()
        finally:
            stop(w)


class TestAM09Recovery:
    def test_a_orphan_host_restarted(self, env):
        """杀 worker → 删容器 → 新 worker recover 判 host_restarted。"""
        env.set_sim("slow", step="1")
        w1 = start(env)
        conn = env.conn()
        run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        conn.close()
        wait_status(env, run["id"], terminal=False)
        time.sleep(2)
        stop(w1)  # 不 stop 容器
        for cid in exec_containers(run["id"]):
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        w2 = start(env)  # 新 worker:recover
        try:
            row = wait_status(env, run["id"], timeout=30)
            assert row["status"] == "failed" and row["error"] == "host_restarted"
            assert w2.health()["alive"] is True
        finally:
            stop(w2)

    def test_b_adopt_running_container(self, env):
        env.set_sim("ok")
        w1 = start(env)
        conn = env.conn()
        run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        conn.close()
        wait_status(env, run["id"], terminal=False)
        stop(w1)  # 容器继续跑
        w2 = start(env)
        try:
            row = wait_status(env, run["id"], timeout=120)
            assert row["status"] == "succeeded"
            assert w2.health()["alive"] is True
        finally:
            stop(w2)

    def test_c_queued_survive_restart(self, env):
        env.set_sim("ok", step="0.3")
        conn = env.conn()
        for i, date in enumerate(("2026-09-10", "2026-09-11")):
            with conn:
                conn.execute(
                    "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date,"
                    " status, trigger, agents_total, created_at)"
                    " VALUES (?,?,?,?,'queued','schedule',11,?)",
                    (f"r-am09c{i}", 1, "market,social,news", date, f"2026-09-14T08:00:{i:02d}+08:00"),
                )
        conn.close()
        w = start(env)
        try:
            deadline = time.time() + 120
            while time.time() < deadline:
                conn = env.conn()
                try:
                    rows = conn.execute("SELECT id,status,started_at FROM run ORDER BY created_at").fetchall()
                finally:
                    conn.close()
                if len(rows) == 2 and all(r["status"] == "succeeded" for r in rows):
                    break
                time.sleep(1)
            assert [r["status"] for r in rows] == ["succeeded", "succeeded"]
            assert [r["started_at"] for r in rows] == sorted(r["started_at"] for r in rows)
        finally:
            stop(w)


class TestAM10DedupForce:
    def test_monday_dedup_and_force(self, env, freezer):
        env.set_sim("ok")
        conn = env.conn()
        try:
            sch_srv.create(conn, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")  # 三件套
            sch_srv.create(conn, instrument_id=1, profile_id=2, kind="weekly", weekday=1, actor="web")  # 全量
        finally:
            conn.close()
        from app.db import connect as _connect

        sched = Scheduler(env.settings, db_factory=lambda: _connect(env.db_path))
        w = start(env)
        c = env.client(w)
        try:
            with freezer("2026-09-14 08:30:00"):  # 周一
                sched._fire(1)
                sched._fire(2)
            conn = env.conn()
            try:
                rows = conn.execute(
                    "SELECT id, analysts_csv FROM run WHERE status IN ('queued','running')"
                ).fetchall()
                assert len(rows) == 1
                assert rows[0]["analysts_csv"] == "market,social,news,fundamentals"
                assert (
                    conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='deduped'").fetchone()["n"]
                    == 1
                )
                rid = rows[0]["id"]
            finally:
                conn.close()
            wait_status(env, rid, timeout=120)
            # b:同日再发 → already_done;force → 新 run
            r409 = c.post("/api/v1/runs", json={"code": "1810.HK", "date": "2026-09-14"})
            assert r409.status_code == 409 and r409.json()["error"] == "already_done"
            rforce = c.post("/api/v1/runs", json={"code": "1810.HK", "date": "2026-09-14", "force": True})
            assert rforce.status_code == 200 and rforce.json()["status"] == "queued"
            rid2 = rforce.json()["id"]
            wait_status(env, rid2, timeout=120)
        finally:
            stop(w)


class TestAM11UpstreamSelfCheck:
    def test_upstream_changed_exit2(self, env):
        env.set_sim("upstream_changed")
        w = start(env)
        try:
            conn = env.conn()
            run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            row = wait_status(env, run["id"], timeout=120)
            assert row["status"] == "failed"
            assert row["exit_code"] == 2
            assert "upstream_api_changed" in (row["error"] or "")
            sf = Path(env.settings.data_dir) / "runs" / run["id"] / "status.json"
            status = read_status(sf)
            assert status.error and status.error.startswith("upstream_api_changed")
        finally:
            stop(w)


class TestAM16AtomicAndTotal:
    def test_status_valid_and_agents_total_dynamic(self, env):
        env.set_sim("ok", step="0.05")
        w = start(env, status_poll_seconds=0.3)
        try:
            conn = env.conn()
            r3 = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
            conn.close()
            sf = Path(env.settings.data_dir) / "runs" / r3["id"] / "status.json"
            samples = 0
            deadline = time.time() + 60
            while time.time() < deadline:
                s = read_status(sf)
                if s is not None:
                    samples += 1
                    assert s.agents_total == 11  # 3 分析师 → 11(AM-16)
                if wait_status(env, r3["id"], terminal=False)["status"] == "succeeded":
                    break
                time.sleep(0.1)
            assert samples >= 2, "采到多个有效 status.json"
            wait_status(env, r3["id"], timeout=60)
            conn = env.conn()
            r4 = runs_srv.create_run(conn, code="NVDA", profile_id=2, trigger="web", actor="web")
            conn.close()
            row4 = wait_status(env, r4["id"], timeout=120)
            assert row4["status"] == "succeeded"
            s4 = read_status(Path(env.settings.data_dir) / "runs" / r4["id"] / "status.json")
            assert s4.agents_total == 12  # 4 分析师 → 12
        finally:
            stop(w)


class TestAM17Whitelist:
    def test_api_param_matrix(self, env):
        w = start(env)
        c = env.client(w)
        try:
            for payload in (
                {"code": "1810.HK", "analysts": ["market", "bogus"]},
                {"code": "1810.HK", "analysts": []},
                {"code": "ABC;rm"},
                {"code": "1810.HK", "date": "2026-13-01"},
                {"code": "1810.HK", "date": "2030-01-01"},
            ):
                r = c.post("/api/v1/runs", json=payload)
                assert r.status_code == 400, payload
                assert r.json()["error"] == "invalid_request"
            # 小写 hk 代码被 normalize 接受
            r_ok = c.post("/api/v1/runs", json={"code": "1810.hk", "profile_id": 1, "date": "2026-09-14"})
            assert r_ok.status_code == 200 and r_ok.json()["code"] == "1810.HK"
            # runner 端非法 analysts 退出码 1
            rp = subprocess.run(
                [
                    ".venv/Scripts/python",
                    "runner/runner.py",
                    "--ticker",
                    "NVDA",
                    "--date",
                    "2026-09-14",
                    "--analysts",
                    "bogus",
                    "--workspace",
                    str(env.tmp / "ws"),
                    "--run-id",
                    "r-bad",
                ],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).resolve().parents[2]),
                timeout=60,
            )
            assert rp.returncode == 1
        finally:
            stop(w)


class TestAM18ContainerLog:
    def test_log_archived_path_only(self, env):
        env.set_sim("fail")
        w = start(env)
        c = env.client(w)
        try:
            rid = c.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1}).json()["id"]
            row = wait_status(env, rid, timeout=120)
            assert row["status"] == "failed"
            log_file = Path(env.settings.data_dir) / "runs" / rid / "container.log"
            assert log_file.is_file()
            text = log_file.read_text(encoding="utf-8")
            assert "sim injected failure" in text  # 含异常栈
            detail = c.get(f"/runs/{rid}").text
            assert "container.log" in detail and "仅路径" in detail
            # 页面渲染的是 run.error(SPEC §7.3 要求全文);container.log 的独有行
            # ([runner] exit= 等)不得出现在页面/API —— 即日志内容不渲染
            for log_marker in ("[runner] exit=", "[runner] failed:"):
                assert log_marker not in detail
            api = c.get(f"/api/v1/runs/{rid}").text
            for log_marker in ("[runner] exit=", "[runner] failed:"):
                assert log_marker not in api
        finally:
            stop(w)
