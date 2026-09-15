"""Worker 全链路单测(FakeLauncher,无 docker;R-EXE-01~12 / AM-01~09)。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import models
from app.db import connect
from app.executor.worker import Worker
from app.services import instruments as inst_srv
from app.services import runs as runs_srv
from tests.conftest import FakeLauncher


@pytest.fixture
def exec_env(tmp_path, make_settings):
    """一套隔离的执行环境:settings + 独立 db + 目录。"""
    s = make_settings(
        data_dir=str(tmp_path / "am"),
        data_host=str(tmp_path / "am"),
        ta_data_dir=str(tmp_path / "ta"),
        ta_data_host=str(tmp_path / "ta"),
        ta_env_host=str(tmp_path / "ta" / ".env"),
        runner_host=str(tmp_path / "runner.py"),
        status_poll_seconds=1,
        stale_minutes=1,
    )
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    s.db_path = str(Path(s.data_dir) / "test.db")  # 覆盖工厂默认,跟随新 data_dir
    Path(s.ta_data_dir).mkdir(parents=True, exist_ok=True)
    (tmp_path / "runner.py").write_text("# runner", encoding="utf-8")
    conn = connect(s.db_path)
    from app import db as app_db

    app_db.migrate(conn)
    inst_srv.create(conn, market="hk", code="1810.HK", actor="web")
    return s, conn


def make_worker(settings, conn, launcher, **overrides):
    """注意:db_factory 每次开新连接(与生产一致);测试自身的 conn 是另一连接。"""
    for k, v in overrides.items():
        setattr(settings, k, v)
    return Worker(settings, launcher=launcher, db_factory=lambda: connect(settings.db_path))


def run_to_end(worker, conn, timeout=30, statuses=("succeeded", "failed", "cancelled")):
    """等 worker 把队列消费到终态;返回最终 run 行。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current, queued = runs_srv.current_and_queue(conn)
        if current is None and not queued:
            rows = runs_srv.list_runs(conn, limit=1)
            if rows and rows[0]["status"] in statuses:
                return rows[0]
        time.sleep(0.1)
    raise TimeoutError("worker 未在期限内完成 run")


class TestWorkerOkFlow:
    def test_ok_succeeded_with_container_log(self, exec_env):
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        w = make_worker(settings, conn, launcher)
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "succeeded"
        assert row["exit_code"] == 0 and row["error"] is None
        assert row["report_ready"] is True
        assert row["container_id"] and row["started_at"] and row["finished_at"]
        # AM-18:container.log 落档且包含日志行;容器已 remove
        log_file = Path(settings.data_dir) / "runs" / row["id"] / "container.log"
        assert log_file.exists() and "[fake] container log line" in log_file.read_text(encoding="utf-8")
        assert launcher.containers == {}

    def test_fail_failed(self, exec_env):
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        w = make_worker(settings, conn, launcher, sim_env="SIM_MODE=fail")
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "failed" and row["exit_code"] == 1
        assert "sim injected failure" in row["error"]

    def test_no_report_double_verdict_failed(self, exec_env):
        """AM-03:退出码 0 但 investment_plan.md 缺失 → failed 并写明缺项。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        w = make_worker(settings, conn, launcher, sim_env="SIM_MODE=no_report")
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "failed"
        assert "investment_plan.md" in row["error"]
        assert row["report_ready"] is False

    def test_no_memory_double_verdict_failed(self, exec_env):
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        w = make_worker(settings, conn, launcher, sim_env="SIM_MODE=no_memory")
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "failed" and "memory" in row["error"]
        assert row["report_ready"] is True  # plan 存在,report_ready 以实况为准


class TestWorkerCancel:
    def test_cancel_running_sigint_130(self, exec_env):
        """AM-04:cancel_requested_at → stop(SIGINT)→ 130 → cancelled。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.7)  # 总时长 > 首个 wait(5),确保取消窗口
        w = make_worker(settings, conn, launcher)
        run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not runs_srv.get(conn, run["id"])["started_at"]:
                time.sleep(0.05)
            runs_srv.request_cancel(conn, run["id"], actor="web")
            row = run_to_end(w, conn, timeout=20)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "cancelled" and row["exit_code"] == 130

    def test_watchdog_timeout(self, exec_env):
        """AM-05:看门狗(缩短阈值)自动取消,error=watchdog_timeout。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.5)
        w = make_worker(settings, conn, launcher, watchdog_minutes=0.03)  # ≈1.8s
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn, timeout=30)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "cancelled"
        assert row["error"] == "watchdog_timeout"


class TestWorkerRobustness:
    def test_launch_failure_marks_failed_thread_alive(self, exec_env):
        """R-EXE-12:docker 启动失败 → run failed,线程不退出。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        launcher.fail_on_start = True
        w = make_worker(settings, conn, launcher)
        runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            row = run_to_end(w, conn)
            assert row["status"] == "failed" and row["error"].startswith("launch_failed")
            assert w.is_alive() is True  # 线程仍活
        finally:
            launcher.fail_on_start = False
            w.stop()
            w.join(timeout=10)

    def test_host_restarted_recovery(self, exec_env):
        """AM-09:孤儿 running,容器不存在 → failed(host_restarted);queued 保留。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.03)
        w = make_worker(settings, conn, launcher)
        # 直接造孤儿:容器 id 从未存在
        with conn:
            conn.execute(
                "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                " trigger, container_id, agents_total, created_at, started_at)"
                " VALUES ('r-orphan', 1, 'market,social,news', '2026-09-14', 'running',"
                " 'web', 'ghost-cid', 11, 't', 't')"
            )
        w._recover_once()
        row = runs_srv.get(conn, "r-orphan")
        assert row["status"] == "failed" and row["error"] == "host_restarted"

    def test_recover_adopts_existing_container(self, exec_env):
        """AM-09:容器仍存在 → 接管监控直至终态。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.05)
        w = make_worker(settings, conn, launcher)
        # 先用真实 FakeLauncher 起一个容器,再把 DB 行造成孤儿
        spec_cid = launcher.start(
            type(
                "S",
                (),
                {
                    "image": "x",
                    "run_id": "r-adopt",
                    "ticker": "1810.HK",
                    "date": "2026-09-14",
                    "analysts": ("market", "social", "news"),
                    "workspace_host": str(Path(settings.data_dir) / "runs" / "r-adopt"),
                    "ta_data_host": settings.ta_data_host,
                    "extra_env": {},
                },
            )()
        )
        with conn:
            conn.execute(
                "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                " trigger, container_id, agents_total, created_at, started_at)"
                " VALUES ('r-adopt', 1, 'market,social,news', '2026-09-14', 'running',"
                " 'web', ?, 11, 't', ?)",
                (spec_cid, models.now_sh().isoformat(timespec="seconds")),
            )
        w.start()
        try:
            row = run_to_end(w, conn, timeout=20)
        finally:
            w.stop()
            w.join(timeout=10)
        assert row["status"] == "succeeded" and row["container_id"] == spec_cid


class TestWorkerProgress:
    def test_corrupt_status_keeps_last_and_marks_stale(self, exec_env):
        """AM-08:status.json 半截 → 保持上次值 + status_stale=1;终态正常。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.05)
        w = make_worker(settings, conn, launcher, sim_env="SIM_MODE=corrupt_status", status_poll_seconds=1)
        run = runs_srv.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        w.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not runs_srv.get(conn, run["id"])["started_at"]:
                time.sleep(0.05)
            # FakeLauncher 的 corrupt 分支写半截 JSON 后停 1.5s,期间轮询应标 stale
            saw_stale = False
            deadline = time.time() + 15
            while time.time() < deadline:
                row = runs_srv.get(conn, run["id"])
                if row["status_stale"]:
                    saw_stale = True
                    break
                if row["status"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.1)
            row = run_to_end(w, conn, timeout=20)
        finally:
            w.stop()
            w.join(timeout=10)
        assert saw_stale, "损坏窗口内必须观察到 status_stale=1"
        assert row["status"] == "succeeded"
        assert row["status_stale"] in (0, 1)  # 终态前最后一次同步的实况

    def test_queue_fifo(self, exec_env):
        """AM-02:三条 queued 按入队顺序串行执行。"""
        settings, conn = exec_env
        launcher = FakeLauncher(step_seconds=0.05)
        w = make_worker(settings, conn, launcher)
        # 绕过忙拒直接造 3 条 queued(不同日期满足唯一索引)
        for i, date in enumerate(("2026-09-10", "2026-09-11", "2026-09-12")):
            with conn:
                conn.execute(
                    "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                    " trigger, agents_total, created_at)"
                    " VALUES (?,?,?,?,'queued','schedule',11,?)",
                    (f"r-q{i}", 1, "market,social,news", date, f"2026-09-14T08:00:{i:02d}+08:00"),
                )
        w.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                rows = runs_srv.list_runs(conn)
                if len(rows) == 3 and all(r["status"] == "succeeded" for r in rows):
                    break
                time.sleep(0.1)
        finally:
            w.stop()
            w.join(timeout=10)
        rows = sorted(runs_srv.list_runs(conn), key=lambda r: r["created_at"])
        started = [r["started_at"] for r in rows]
        assert started == sorted(started), "FIFO:按 created_at 顺序启动"
        assert all(r["status"] == "succeeded" for r in rows)

    def test_health_shape(self, exec_env):
        settings, conn = exec_env
        w = make_worker(settings, conn, FakeLauncher())
        h = w.health()
        assert set(h) == {"alive", "docker_ok", "queue_depth", "current_run_id", "last_tick"}


class TestSelfCheck:
    def test_missing_dirs_reported(self, exec_env):
        settings, conn = exec_env
        settings.ta_data_dir = str(Path(settings.ta_data_dir) / "missing")
        w = make_worker(settings, conn, FakeLauncher())
        errors = w.self_check()
        assert any("AM_TA_DATA_DIR" in e for e in errors)

    def test_ping_failure_reported(self, exec_env):
        settings, conn = exec_env
        launcher = FakeLauncher()
        launcher.ping_ok = False
        w = make_worker(settings, conn, launcher)
        errors = w.self_check()
        assert any("docker" in e for e in errors)

    def test_ok(self, exec_env):
        settings, conn = exec_env
        w = make_worker(settings, conn, FakeLauncher())
        assert w.self_check() == []
