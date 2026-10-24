"""v1.2 财报定时增量 + 新标的引导 单测。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.db import connect
from app.reports import sync as reports_sync
from app.scheduler import REPORTS_SYNC_JOB_ID, Scheduler
from app.services import instruments as inst_srv
from app.services import report_jobs as rj_srv
from app.web.server import create_app


@pytest.fixture
def env(tmp_path, make_settings):
    s = make_settings(
        data_dir=str(tmp_path / "am"),
        data_host=str(tmp_path / "am"),
        ta_data_dir=str(tmp_path / "ta"),
        ta_data_host=str(tmp_path / "ta"),
        reports_base_url="http://reports-mock:8080",
    )  # 功能开启
    from pathlib import Path

    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    s.db_path = str(Path(s.data_dir) / "t.db")
    conn = connect(s.db_path)
    app_db.migrate(conn)
    a = inst_srv.create(conn, market="us", code="NVDA", actor="web")  # id=1
    b = inst_srv.create(conn, market="hk", code="0700.HK", actor="web")  # id=2
    c = inst_srv.create(conn, market="cn", code="600519.SS", actor="web")  # id=3
    yield s, conn, (a, b, c)
    conn.close()


_seed_seq = iter(range(1, 1000))


def seed_job(conn, inst, *, status="succeeded", trigger="web", last_n=12):
    n = next(_seed_seq)
    with conn:
        conn.execute(
            "INSERT INTO report_job(id, instrument_id, market, code, symbol, last_n, refresh,"
            " idempotency_key, status, trigger, submit_attempts, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                f"rj-t{n}-{inst['code']}-{status}-{trigger}",
                inst["id"],
                inst["market"],
                inst["code"],
                inst["code"],
                last_n,
                0,
                f"k{n}-{inst['code']}-{trigger}",
                status,
                trigger,
                "2026-10-24T10:00:00+08:00",
                "2026-10-24T10:00:00+08:00",
            ),
        )


class TestTriggerSchedule:
    def test_create_accepts_schedule_trigger(self, env):
        s, conn, (a, _, _) = env
        job = rj_srv.create(
            conn,
            code=a["code"],
            market=a["market"],
            last_n=2,
            refresh=True,
            trigger="schedule",
            actor="schedule",
            default_last_n=s.reports_last_n_default,
        )
        assert job["trigger"] == "schedule" and job["refresh"] is True
        assert job["last_n"] == 2

    def test_old_db_rebuild_allows_schedule(self, tmp_path, make_settings):
        """老 CHECK(web,api)库经 migrate 重建后可插 schedule,数据保留。"""
        from app.db import _REPORT_JOB_DDL  # noqa: F401  确认可导入

        s = make_settings()
        from pathlib import Path

        Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(s.db_path)
        raw.execute("""CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY,
            name TEXT UNIQUE, analysts_csv TEXT, is_default INTEGER)""")
        raw.execute("""CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY,
            instrument_id INTEGER, profile_id INTEGER, kind TEXT, at_time TEXT,
            weekday INTEGER, enabled INTEGER, seq INTEGER)""")
        raw.execute("""CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, ts TEXT,
            actor TEXT, action TEXT, entity TEXT, entity_id TEXT, detail_json TEXT)""")
        raw.execute("""CREATE TABLE instrument (id INTEGER PRIMARY KEY, market TEXT, code TEXT,
            name TEXT, enabled INTEGER, created_at TEXT, updated_at TEXT)""")
        raw.execute("""CREATE TABLE report_job (
            id TEXT PRIMARY KEY, instrument_id INTEGER, market TEXT, code TEXT, symbol TEXT,
            last_n INTEGER, refresh INTEGER, idempotency_key TEXT UNIQUE, remote_job_id TEXT,
            status TEXT, trigger TEXT CHECK(trigger IN ('web','api')), submit_attempts INTEGER,
            next_attempt_at TEXT, progress_json TEXT, summary_json TEXT, results_json TEXT,
            warnings_json TEXT, report_ids_json TEXT, error TEXT, error_code TEXT,
            error_retryable INTEGER, created_at TEXT, started_at TEXT, finished_at TEXT,
            updated_at TEXT)""")
        raw.execute("""CREATE INDEX idx_report_job_status ON report_job(status)""")
        raw.execute("""INSERT INTO report_job (id, market, code, symbol, last_n, refresh,
            idempotency_key, status, trigger, submit_attempts, created_at, updated_at)
            VALUES ('rj-old','us','MU','MU',4,0,'k-old','succeeded','web',0,'t','t')""")
        raw.commit()
        raw.close()
        conn = connect(s.db_path)
        app_db.migrate(conn)
        assert conn.execute("SELECT COUNT(*) AS n FROM report_job").fetchone()["n"] == 1
        conn.execute("""INSERT INTO report_job (id, market, code, symbol, last_n, refresh,
            idempotency_key, status, trigger, created_at, updated_at)
            VALUES ('rj-new','us','MU','MU',2,1,'k-new','pending','schedule','t','t')""")
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_report_job_status'"
        ).fetchone()
        app_db.migrate(conn)  # 幂等
        conn.close()


class TestSyncCandidates:
    def test_only_backfilled_enabled_idle(self, env):
        """启用 + 已回填 + 无活动任务 → 候选;缺一即排除。"""
        s, conn, (a, b, c) = env
        seed_job(conn, a)  # a 回填
        seed_job(conn, b, status="failed")  # b 仅失败 → 未回填
        inst_srv.update(conn, c["id"], enabled=False, actor="web")  # c 停用
        assert [x["code"] for x in rj_srv.sync_candidates(conn)] == ["NVDA"]
        seed_job(conn, b)  # b 补成功 → 候选
        assert [x["code"] for x in rj_srv.sync_candidates(conn)] == ["NVDA", "0700.HK"]
        # a 已有活动任务 → 排除
        with conn:
            conn.execute(
                "INSERT INTO report_job(id, instrument_id, market, code, symbol, last_n,"
                " refresh, idempotency_key, status, trigger, created_at, updated_at)"
                " VALUES ('rj-act', ?, 'us', 'NVDA', 'NVDA', 2, 1, 'k-act', 'pending',"
                " 'schedule', 't', 't')",
                (a["id"],),
            )
        assert [x["code"] for x in rj_srv.sync_candidates(conn)] == ["0700.HK"]

    def test_create_sync_jobs(self, env):
        s, conn, (a, b, _) = env
        seed_job(conn, a)
        seed_job(conn, b)
        jobs = rj_srv.create_sync_jobs(conn, settings=s)
        assert {j["code"] for j in jobs} == {"NVDA", "0700.HK"}
        for j in jobs:
            assert j["trigger"] == "schedule" and j["refresh"] is True
            assert j["last_n"] == s.reports_sync_last_n
        # 再跑一次:全有活动任务 → 空
        assert rj_srv.create_sync_jobs(conn, settings=s) == []

    def test_run_daily_sync_disabled_noop(self, tmp_path, make_settings):
        s = make_settings()  # 未配置 reports_base_url
        conn = connect(":memory:")
        app_db.migrate(conn)
        from app.db import connect as _c

        n = reports_sync.run_daily_sync(s, lambda: _c(":memory:"))
        assert n == 0
        conn.close()


class TestSchedulerRegistration:
    def test_registered_when_enabled(self, env):
        s, conn, _ = env
        sched = Scheduler(s, db_factory=lambda: connect(s.db_path))
        sched.rebuild_jobs()
        job = sched._scheduler.get_job(REPORTS_SYNC_JOB_ID)
        assert job is not None
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields.get("hour") == "6" and fields.get("minute") == "0"
        assert fields.get("day_of_week") == "*"

    def test_custom_time_and_disabled(self, env, make_settings):
        s, conn, _ = env

        def fresh_db():
            c = connect(":memory:")
            app_db.migrate(c)
            return c

        s2 = make_settings(reports_sync_at="07:30")  # 未配置 base_url → disabled
        sched2 = Scheduler(s2, db_factory=fresh_db)
        sched2.rebuild_jobs()
        assert sched2._scheduler.get_job(REPORTS_SYNC_JOB_ID) is None
        s3 = make_settings(reports_sync_at="07:30", reports_base_url="http://x:8000")
        sched3 = Scheduler(s3, db_factory=fresh_db)
        sched3.rebuild_jobs()
        job = sched3._scheduler.get_job(REPORTS_SYNC_JOB_ID)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields.get("hour") == "7" and fields.get("minute") == "30"


class NoopWorker:
    def self_check(self):
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"alive": True, "docker_ok": None, "queue_depth": 0, "current_run_id": None, "last_tick": None}


@pytest.fixture
def client(env):
    s, conn, _ = env
    app = create_app(s, worker=NoopWorker(), scheduler=Scheduler(s, db_factory=lambda: connect(s.db_path)))
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {s.token}"})
        yield c


class TestNewInstrumentPrompt:
    def test_prompt_after_create(self, client):
        r = client.post("/fragments/instruments", data={"market": "us", "code": "TSLA", "name": "Tesla"})
        assert r.status_code == 200
        assert 'id="sync-prompt"' in r.text
        assert 'value="12"' in r.text  # 默认期数(REPORTS_BACKFILL_LAST_N_DEFAULT)
        import re as _re

        assert _re.search(r"/fragments/instruments/\d+/report-sync", r.text)
        assert "report-sync-dismiss" in r.text

    def test_sync_creates_job_with_custom_n(self, client, env):
        s, conn, _ = env
        inst = inst_srv.create(conn, market="us", code="AMD", actor="web")
        r = client.post(f"/fragments/instruments/{inst['id']}/report-sync", data={"last_n": "8"})
        assert r.status_code == 200 and "已创建财报任务" in r.text
        assert 'id="sync-prompt"' not in r.text  # 卡片消失
        job = conn.execute(
            "SELECT last_n, trigger, refresh, code FROM report_job WHERE instrument_id=?", (inst["id"],)
        ).fetchone()
        assert (job["last_n"], job["trigger"], job["refresh"], job["code"]) == (8, "web", 0, "AMD")

    def test_sync_default_n_and_bad_n(self, client, env):
        s, conn, _ = env
        inst = inst_srv.create(conn, market="hk", code="3690.HK", actor="web")
        r = client.post(f"/fragments/instruments/{inst['id']}/report-sync", data={})
        assert "最近 12 期" in r.text  # 缺省 → 12
        inst3 = inst_srv.create(conn, market="us", code="INTC", actor="web")
        r2 = client.post(f"/fragments/instruments/{inst3['id']}/report-sync", data={"last_n": "99"})
        assert "flash-error" in r2.text  # 99 超范围 → 内联错误

    def test_dismiss(self, client, env):
        _, conn, _ = env
        inst = inst_srv.create(conn, market="us", code="QCOM", actor="web")
        r = client.post(f"/fragments/instruments/{inst['id']}/report-sync-dismiss")
        assert r.status_code == 200 and 'id="sync-prompt"' not in r.text

    def test_no_prompt_when_reports_disabled(self, tmp_path, make_settings):
        s = make_settings()  # base_url 空
        from pathlib import Path

        Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = connect(s.db_path)
        app_db.migrate(conn)
        app = create_app(
            s, worker=NoopWorker(), scheduler=Scheduler(s, db_factory=lambda: connect(s.db_path))
        )
        with TestClient(app) as c:
            c.headers.update({"Authorization": f"Bearer {s.token}"})
            r = c.post("/fragments/instruments", data={"market": "us", "code": "TSLA", "name": ""})
            assert 'id="sync-prompt"' not in r.text
