"""LOCAL 静态/进程级验收:AM-06 绑定矩阵 / AM-12 交易日语义 / AM-13 审计与实况 /
AM-14 无 npm/外链 / AM-15 轮询无 WS。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.scheduler import Scheduler
from app.services import profiles as prof_srv
from app.services import schedules as sch_srv
from tests.acceptance.harness import AcceptanceEnv, requires_sim, wait_status

REPO = Path(__file__).resolve().parents[2]
VENV_PY = str(REPO / ".venv" / "Scripts" / "python")

pytestmark = pytest.mark.acceptance


def _run_manager(env: dict, timeout: float = 25) -> subprocess.CompletedProcess:
    """以给定环境启动 python -m app.main,等退出或超时;超时则 kill(视为正常启动)。"""
    proc = subprocess.Popen(
        [VENV_PY, "-m", "app.main"],
        env=env,
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess([], proc.returncode, stdout=out)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return subprocess.CompletedProcess([], "STARTED_OK", stdout=out)


class TestAM06BindMatrix:
    REQUIRED = {
        "AM_TA_DATA_DIR": "./.local/ta-data",
        "AM_TA_DATA_HOST": "./.local/ta-data",
        "AM_TA_ENV_HOST": "./.local/ta.env",
        "AM_RUNNER_HOST": "./runner/runner.py",
        "AM_DATA_DIR": "./.local/am-data",
        "AM_TA_IMAGE": "tradingagents-sim:latest",  # 本地自检需要本地镜像
        "AM_PORT": "8097",
    }

    def _env(self, bind: str, token: str) -> dict:
        e = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("AM_") and k not in ("HTTP_PROXY", "HTTPS_PROXY")
        }
        e.update(self.REQUIRED)
        e["AM_BIND"] = bind
        if token:
            e["AM_TOKEN"] = token
        return e

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.150", "0.0.0.0 "])
    @pytest.mark.parametrize("token", ["", "t" * 31])
    def test_non_loopback_refused(self, bind, token):
        r = _run_manager(self._env(bind, token), timeout=15)
        assert r.returncode == 2, f"bind={bind!r} token_len={len(token)}: {r.stdout[-300:]}"

    def test_loopback_no_token_starts(self):
        r = _run_manager(self._env("127.0.0.1", ""), timeout=8)
        assert r.returncode == "STARTED_OK" or r.returncode == 0

    def test_any_bind_with_valid_token_starts_and_no_token_in_log(self):
        token = "x" * 40
        r = _run_manager(self._env("0.0.0.0", token), timeout=8)
        assert r.returncode == "STARTED_OK" or r.returncode == 0
        assert token not in (r.stdout or "")


class TestAM12TradingDays:
    def test_weekend_no_fire_weekday_fires(self, tmp_path, make_settings, freezer):
        e = AcceptanceEnv(tmp_path, make_settings)
        conn = e.conn()
        try:
            sch_srv.create(conn, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")
        finally:
            conn.close()
        # 语义:周末 cron 触发器不会调度该作业(next fire 落到周一);
        # 工作日到点触发 → 入队一条。_fire 本身不做日历判断(由 CronTrigger 保证)。
        from datetime import date as _date

        from app.db import connect as _c

        sched = Scheduler(e.settings, db_factory=lambda: _c(e.db_path))
        sched.rebuild_jobs()
        with freezer("2026-09-12 08:00:00"):  # 周六上午
            nxt = sched.job_next_fire("am-sched-1")
            assert nxt is not None and nxt.weekday() == 0  # 下一个触发=周一
            assert sched.next_fires(_date(2026, 9, 12)) == []
        with freezer("2026-09-13 22:00:00"):  # 周日深夜
            assert sched.next_fires(_date(2026, 9, 13)) == []
        with freezer("2026-09-14 08:00:00"):  # 周一
            fires = sched.next_fires(_date(2026, 9, 14))
            assert len(fires) == 1 and fires[0]["code"] == "1810.HK"
            sched._fire(1)  # 工作日到点,作业被调用 → 入队
        conn = e.conn()
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1
        finally:
            conn.close()


class TestAM13AuditAndReportTruth:
    @requires_sim
    def test_audit_actors_and_artifacts_truth(self, tmp_path, make_settings):
        e = AcceptanceEnv(tmp_path, make_settings)
        e.set_sim("ok")
        w = e.worker()
        w.start()
        try:
            api_client = e.client(w)  # Bearer → actor=api
            # 页面会话(cookie)→ actor=web
            page_client = e.client(w)
            page_client.headers.pop("Authorization")
            login = page_client.post("/login", data={"token": e.settings.token}, follow_redirects=False)
            assert login.status_code == 303 and "am_session" in login.cookies
            client = page_client  # 下面的页面操作走 cookie 会话
            # 标的增(页面)/改/删 + 档案增 + 调度增/启停/删,各产生 audit
            r = client.post("/fragments/instruments", data={"market": "us", "code": "TSLA", "name": "Tesla"})
            assert r.status_code == 200
            r2 = client.post("/fragments/instruments/4", data={"name": "Tesla Inc", "enabled": "1"})
            assert r2.status_code == 200
            r3 = client.post(
                "/fragments/schedules",
                data={"instrument_id": "4", "profile_id": "1", "kind": "daily_trading", "at_time": "09:00"},
            )
            assert r3.status_code == 200
            r4 = client.post("/fragments/schedules/1/toggle")
            assert r4.status_code == 200
            r5 = client.delete("/fragments/schedules/1")
            assert r5.status_code == 200
            r6 = client.delete("/fragments/instruments/4")
            assert r6.status_code == 200
            conn = e.conn()
            try:
                prof_srv.create(conn, name="自定义", analysts_csv="market,news", actor="web")
            finally:
                conn.close()
            # 手动发起(API,Bearer)→ 成功
            rid = api_client.post(
                "/api/v1/runs", json={"code": "1810.HK", "profile_id": 1, "date": "2026-09-14"}
            ).json()["id"]
            wait_status(e, rid, timeout=180)
            arts = api_client.get(f"/api/v1/runs/{rid}/artifacts").json()["paths"]
            assert any(
                p.endswith("/investment_plan.md") or p.endswith("reports/investment_plan.md") for p in arts
            )
            # 删除 investment_plan.md → artifacts 不再包含(实况为准;report_ready
            # 终态后不回写,以 artifacts 分支满足 AM-13,记录中注明)
            date = "2026-09-14"
            (
                Path(e.settings.ta_data_dir) / "logs" / "1810.HK" / date / "reports" / "investment_plan.md"
            ).unlink()
            arts2 = api_client.get(f"/api/v1/runs/{rid}/artifacts").json()["paths"]
            assert not any(
                p.rstrip("/").endswith("/investment_plan.md")
                or p == "logs/1810.HK/2026-09-14/reports/investment_plan.md"
                for p in arts2
            )
            # 审计 actor 与来源一致
            conn = e.conn()
            try:
                rows = conn.execute("SELECT actor, action, entity FROM audit_log ORDER BY id").fetchall()
            finally:
                conn.close()
            by_action = {(r["entity"], r["action"]): r["actor"] for r in rows}
            assert by_action[("instrument", "create")] == "web"
            assert by_action[("instrument", "update")] == "web"
            assert by_action[("schedule", "create")] == "web"
            assert by_action[("schedule", "toggle")] == "web"
            assert by_action[("schedule", "delete")] == "web"
            assert by_action[("instrument", "delete")] == "web"
            assert by_action[("profile", "create")] == "web"
            assert by_action[("run", "create_run")] == "api"  # 经 API 发起
        finally:
            w.stop()
            w.join(timeout=20)


class TestAM14NoNpmCdn:
    MANAGER_IMAGE = "research-manage-agents-manage"

    def test_no_node_in_image(self):
        r = subprocess.run(
            ["docker", "image", "inspect", self.MANAGER_IMAGE], capture_output=True, text=True, timeout=20
        )
        if r.returncode != 0:
            pytest.skip(f"缺少 {self.MANAGER_IMAGE}(docker compose -f deploy/docker-compose.local.yml build)")
        w = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                self.MANAGER_IMAGE,
                "-c",
                "which node npm; echo rc=$?",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "rc=1" in w.stdout  # which 两条都找不到
        assert "node_modules" not in w.stdout

    def test_templates_static_no_external_links(self):
        targets = list((REPO / "app" / "web" / "templates").rglob("*.html")) + list(
            (REPO / "app" / "web" / "static").glob("*.css")
        )
        assert targets
        for f in targets:
            text = f.read_text(encoding="utf-8")
            assert not re.search(r"https?://", text), f

    def test_local_assets_only(self, tmp_path):
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.db import connect as _c
        from app.scheduler import Scheduler
        from app.web.server import create_app

        class NoopWorker:
            def self_check(self):
                return []

            def start(self):
                pass

            def stop(self):
                pass

            def health(self):
                return {
                    "alive": True,
                    "docker_ok": None,
                    "queue_depth": 0,
                    "current_run_id": None,
                    "last_tick": None,
                }

        settings = Settings(
            bind="127.0.0.1",
            port=8090,
            token="t" * 40,
            data_dir=str(tmp_path / "am"),
            data_host=str(tmp_path / "am"),
            ta_data_dir=str(tmp_path / "ta"),
            ta_data_host=str(tmp_path / "ta"),
            ta_env_host=str(tmp_path / "ta" / ".env"),
            runner_host=str(tmp_path / "r.py"),
        )
        settings.db_path = str(tmp_path / "am" / "t.db")
        Path(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
        app = create_app(
            settings,
            worker=NoopWorker(),
            scheduler=Scheduler(settings, db_factory=lambda: _c(settings.db_path)),
        )
        with TestClient(app) as c:
            c.headers.update({"Authorization": f"Bearer {settings.token}"})
            page = c.get("/")
            assert 'src="/static/htmx.min.js"' in page.text
            assert c.get("/static/htmx.min.js").status_code == 200


class TestAM15PollingNoWs:
    def test_fragments_carry_every_15s_and_no_ws_code(self):
        for py in (REPO / "app").rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for pat in ("WebSocket", "EventSource", "text/event-stream"):
                assert pat not in text, f"{py.name} 含 {pat}"
        frag = (REPO / "app" / "web" / "templates" / "fragments" / "overview.html").read_text(
            encoding="utf-8"
        )
        assert 'hx-trigger="every 15s"' in frag
