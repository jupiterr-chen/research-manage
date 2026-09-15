"""Worker + 真实 docker + sim 镜像集成测试(R-EXE-01~12 / AM-01~09)。

运行:pytest -m docker tests/integration/test_worker_sim.py
覆盖:ok / fail / hang+cancel / watchdog(缩短阈值)/ no_memory / no_report /
corrupt_status / resume / host_restarted / 队列 FIFO。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from app import db as app_db
from app import models
from app.db import connect
from app.executor.launcher import DockerLauncher
from app.executor.worker import Worker
from app.services import instruments as inst_srv
from app.services import runs as runs_srv

REPO = Path(__file__).resolve().parents[2]
RUNNER_HOST = REPO / "runner" / "runner.py"
IMAGE = "tradingagents-sim:latest"
TICKER = "1810.HK"
DATE = "2026-09-14"


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _image_exists() -> bool:
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True, timeout=20)
    return r.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="本地 Docker 不可用"),
    pytest.mark.skipif(not _image_exists(), reason=f"缺少 {IMAGE},先运行 scripts/build_sim.sh"),
]


def _win(p) -> str:
    return str(p).replace("\\", "/")


def set_sim(env, mode: str | None) -> None:
    """AM_SIM_ENV 注入:SIM_MODE + 断点开关(生产由挂载的 .env 提供,sim 不读 .env)。"""
    parts = ["TRADINGAGENTS_CHECKPOINT_ENABLED=true"]
    if mode:
        parts.append(f"SIM_MODE={mode}")
    env[0].sim_env = ",".join(parts)


@pytest.fixture
def env(tmp_path, make_settings):
    """Worker 生产形态:DockerLauncher + sim 镜像 + AM_SIM_ENV 注入 SIM_MODE。"""
    s = make_settings(
        data_dir=str(tmp_path / "am"),
        data_host=str(tmp_path / "am"),
        ta_data_dir=str(tmp_path / "ta"),
        ta_data_host=str(tmp_path / "ta"),
        ta_env_host=str(tmp_path / "ta" / ".env"),
        runner_host=str(RUNNER_HOST),
        ta_image=IMAGE,
        status_poll_seconds=1,
        stale_minutes=1,
    )
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    s.db_path = str(Path(s.data_dir) / "test.db")
    Path(s.ta_data_dir).mkdir(parents=True, exist_ok=True)
    Path(s.ta_env_host).write_text("# fake env(管理台不读)\n", encoding="utf-8")
    conn = connect(s.db_path)
    app_db.migrate(conn)
    inst_srv.create(conn, market="hk", code=TICKER, actor="web")
    return s, conn


def start_worker(env, **overrides) -> Worker:
    settings, _ = env
    for k, v in overrides.items():
        setattr(settings, k, v)
    w = Worker(settings, launcher=DockerLauncher(), db_factory=lambda: connect(settings.db_path))
    w.start()
    return w


def wait_terminal(conn, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = runs_srv.get(conn, run_id)
        if row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        time.sleep(0.3)
    raise TimeoutError(f"run {run_id} 未到终态")


class TestWorkerDocker:
    def test_ok_succeeded_with_log(self, env):
        settings, conn = env
        settings.sim_env = "TRADINGAGENTS_CHECKPOINT_ENABLED=true"
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            row = wait_terminal(conn, run["id"])
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "succeeded" and row["error"] is None
        assert row["exit_code"] == 0 and row["report_ready"] is True
        log_file = Path(settings.data_dir) / "runs" / run["id"] / "container.log"
        assert log_file.exists() and log_file.read_text(encoding="utf-8").strip()
        # 容器已 remove:无 am-<run_id> 残留
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=am-{run['id']}", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not r.stdout.strip()

    def test_fail_failed(self, env):
        _, conn = env
        set_sim(env, "fail")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            row = wait_terminal(conn, run["id"])
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "failed" and row["exit_code"] == 1
        assert "sim injected failure" in (row["error"] or "")

    def test_hang_cancel_sigint_130(self, env):
        """AM-04:hang 模式 + cancel → SIGINT stop → cancelled / 130,断点保留。"""
        _, conn = env
        set_sim(env, "hang")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not runs_srv.get(conn, run["id"])["started_at"]:
                time.sleep(0.3)
            time.sleep(4)  # 走到第 3 节点挂起
            runs_srv.request_cancel(conn, run["id"], actor="web")
            row = wait_terminal(conn, run["id"], timeout=60)
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "cancelled" and row["exit_code"] == 130
        cps = list((Path(env[0].ta_data_dir) / "cache" / "checkpoints").glob("*.json"))
        assert cps, "断点保留供 resume"

    def test_watchdog_shortened(self, env):
        """AM-05:看门狗(缩短到 ~6s)自动取消,error=watchdog_timeout。"""
        _, conn = env
        set_sim(env, "hang")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env, watchdog_minutes=0.1)  # 6s
        try:
            row = wait_terminal(conn, run["id"], timeout=90)
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "cancelled"
        assert row["error"] == "watchdog_timeout"

    def test_no_memory_double_verdict(self, env):
        _, conn = env
        set_sim(env, "no_memory")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            row = wait_terminal(conn, run["id"])
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "failed" and "memory" in row["error"]

    def test_no_report_double_verdict(self, env):
        """AM-03:退出码 0 但 investment_plan.md 缺失 → failed。"""
        _, conn = env
        set_sim(env, "no_report")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            row = wait_terminal(conn, run["id"])
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "failed" and "investment_plan.md" in row["error"]

    def test_corrupt_status_marks_stale(self, env):
        """AM-08:status.json 半截 → status_stale=1,终态不受影响。"""
        settings, conn = env
        set_sim(env, "corrupt_status")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env, status_poll_seconds=1)
        try:
            saw_stale = False
            deadline = time.time() + 30
            while time.time() < deadline:
                row = runs_srv.get(conn, run["id"])
                if row["status_stale"]:
                    saw_stale = True
                if row["status"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.2)
            row = wait_terminal(conn, run["id"])
        finally:
            w.stop()
            w.join(timeout=15)
        assert saw_stale, "损坏窗口内应观察到 status_stale=1"
        assert row["status"] == "succeeded"

    def test_resume_after_cancel(self, env):
        """AM-04:取消后 resume → 新 run 跑完,断点清零。"""
        _, conn = env
        set_sim(env, "hang")
        run = runs_srv.create_run(conn, code=TICKER, profile_id=1, trigger="web", actor="web")
        w = start_worker(env)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not runs_srv.get(conn, run["id"])["started_at"]:
                time.sleep(0.3)
            time.sleep(4)
            runs_srv.request_cancel(conn, run["id"], actor="web")
            wait_terminal(conn, run["id"], timeout=60)
            set_sim(env, "ok")
            run2 = runs_srv.resume(conn, run["id"], actor="web")
            row2 = wait_terminal(conn, run2["id"], timeout=90)
        finally:
            w.stop()
            w.join(timeout=15)
        assert row2["status"] == "succeeded"
        assert row2["resumed_from"] == run["id"]
        assert not list((Path(env[0].ta_data_dir) / "cache" / "checkpoints").glob("*.json"))

    def test_host_restarted(self, env):
        """AM-09:DB 有 running 但容器不存在 → failed(host_restarted)。"""
        _, conn = env
        with conn:
            conn.execute(
                "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                " trigger, container_id, agents_total, created_at, started_at)"
                " VALUES ('r-ghost', 1, 'market,social,news', ?, 'running', 'web',"
                " 'ghost-cid-not-exist', 11, 't', ?)",
                (DATE, models.now_sh().isoformat(timespec="seconds")),
            )
        w = start_worker(env)
        try:
            deadline = time.time() + 15
            row = runs_srv.get(conn, "r-ghost")
            while time.time() < deadline and row["status"] == "running":
                time.sleep(0.3)
                row = runs_srv.get(conn, "r-ghost")
        finally:
            w.stop()
            w.join(timeout=15)
        assert row["status"] == "failed" and row["error"] == "host_restarted"

    def test_queue_fifo_serial(self, env):
        """AM-02:两条 queued 按 created_at 顺序串行完成。"""
        _, conn = env
        set_sim(env, "ok")
        for i, date in enumerate(("2026-09-10", "2026-09-11")):
            with conn:
                conn.execute(
                    "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                    " trigger, agents_total, created_at)"
                    " VALUES (?,?,?,?,'queued','schedule',11,?)",
                    (f"r-serial{i}", 1, "market,social,news", date, f"2026-09-14T08:00:{i:02d}+08:00"),
                )
        w = start_worker(env)
        try:
            deadline = time.time() + 120
            while time.time() < deadline:
                rows = [runs_srv.get(conn, f"r-serial{i}") for i in range(2)]
                if all(r["status"] == "succeeded" for r in rows):
                    break
                time.sleep(0.5)
        finally:
            w.stop()
            w.join(timeout=15)
        rows = [runs_srv.get(conn, f"r-serial{i}") for i in range(2)]
        assert all(r["status"] == "succeeded" for r in rows)
        assert rows[0]["finished_at"] <= rows[1]["finished_at"], "串行:FIFO 完成顺序"
