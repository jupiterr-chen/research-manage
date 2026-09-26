"""/api/v1/runs* 单测(R-API-01~05 / AM-01/10/17)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.scheduler import Scheduler
from app.services import instruments as inst_srv
from app.web.server import create_app


class NoopWorker:
    """API 测试用:不消费队列,保持 DB 状态确定。"""

    def self_check(self):
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"alive": True, "docker_ok": None, "queue_depth": 0, "current_run_id": None, "last_tick": None}


@pytest.fixture
def api_client(settings):
    Path(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings,
        worker=NoopWorker(),
        scheduler=Scheduler(settings, db_factory=lambda: connect(settings.db_path)),
    )
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {settings.token}"})
        yield c


@pytest.fixture
def dbconn(settings, api_client):
    conn = connect(settings.db_path)
    inst_srv.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")
    inst_srv.create(conn, market="us", code="NVDA", actor="web")
    yield conn
    conn.close()


def seed_run(conn, instrument_id, *, status="queued", date="2026-09-14", csv="market,social,news", rid=None):
    rid = rid or f"r-seed-{status}-{instrument_id}-{date}"
    with conn:
        conn.execute(
            "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
            " trigger, agents_total, created_at, started_at)"
            " VALUES (?,?,?,?,?,'web',11,'2026-09-14T08:00:00+08:00','2026-09-14T08:01:00+08:00')",
            (rid, instrument_id, csv, date, status),
        )
    return rid


class TestCreateRun:
    def test_ok(self, api_client, dbconn):
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued" and body["trigger"] == "api"
        assert body["analysts"] == ["market", "social", "news"]
        assert body["agents_total"] == 11
        # actor=api 审计
        row = dbconn.execute("SELECT actor, action FROM audit_log WHERE entity='run'").fetchone()
        assert row["actor"] == "api" and row["action"] == "create_run"

    def test_busy_409_shape(self, api_client, dbconn):
        """AM-01:忙时 409,body 含 current{六字段} 与 queued,零副作用。"""
        seed_run(dbconn, 1, status="running", rid="r-busy-1")
        seed_run(dbconn, 2, status="queued", date="2026-09-13", rid="r-busy-2")
        before = dbconn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1})
        assert r.status_code == 409
        body = r.json()
        assert body["error"] == "busy"
        assert set(body["current"]) == {
            "id",
            "code",
            "date",
            "current_agent",
            "agents_done",
            "agents_total",
            "started_at",
        }
        assert body["current"]["id"] == "r-busy-1" and body["current"]["code"] == "1810.HK"
        assert body["queued"] == 1
        after = dbconn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        assert before == after  # 零副作用

    def test_already_done_409_and_force(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="succeeded", rid="r-done-1")
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "profile_id": 1, "date": "2026-09-14"})
        assert r.status_code == 409
        assert r.json()["error"] == "already_done"
        assert r.json()["run_id"] == rid
        r2 = api_client.post(
            "/api/v1/runs", json={"code": "1810.HK", "profile_id": 1, "date": "2026-09-14", "force": True}
        )
        assert r2.status_code == 200 and r2.json()["status"] == "queued"

    def test_invalid_code_400(self, api_client, dbconn):
        """AM-17:非法代码格式 → 400。"""
        for code in ("", "bad code!", "工具长代码工具长代码", "12345678901234567890"):
            r = api_client.post("/api/v1/runs", json={"code": code})
            assert r.status_code in (400, 422), code
            assert r.json()["error"] in ("invalid_request",)

    def test_unknown_code_404(self, api_client, dbconn):
        r = api_client.post("/api/v1/runs", json={"code": "0700.HK"})
        assert r.status_code == 404 and r.json()["error"] == "not_found"

    def test_invalid_analysts_400(self, api_client, dbconn):
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "analysts": ["bogus"]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_request"
        r2 = api_client.post("/api/v1/runs", json={"code": "1810.HK", "analysts": []})
        assert r2.status_code == 400

    def test_analysts_and_profile_conflict_400(self, api_client, dbconn):
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "analysts": ["market"], "profile_id": 1})
        assert r.status_code == 400

    def test_future_date_400(self, api_client, dbconn):
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK", "date": "2030-01-01"})
        assert r.status_code == 400

    def test_unauthorized_401(self, api_client, settings):
        api_client.headers.pop("Authorization")
        r = api_client.post("/api/v1/runs", json={"code": "1810.HK"})
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"


class TestQueries:
    def test_list_filters(self, api_client, dbconn):
        seed_run(dbconn, 1, status="succeeded", rid="a1", date="2026-09-10")
        seed_run(dbconn, 1, status="failed", rid="a2", date="2026-09-11")
        seed_run(dbconn, 2, status="queued", rid="a3", date="2026-09-12")
        r = api_client.get("/api/v1/runs")
        assert r.status_code == 200 and len(r.json()["runs"]) == 3
        r2 = api_client.get("/api/v1/runs", params={"status": "failed"})
        assert [x["id"] for x in r2.json()["runs"]] == ["a2"]
        r3 = api_client.get("/api/v1/runs", params={"code": "NVDA"})
        assert [x["id"] for x in r3.json()["runs"]] == ["a3"]
        r4 = api_client.get("/api/v1/runs", params={"limit": 2})
        assert len(r4.json()["runs"]) == 2
        r5 = api_client.get("/api/v1/runs", params={"status": "bogus"})
        assert r5.status_code == 400

    def test_detail_fields(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="running", rid="r-detail")
        r = api_client.get(f"/api/v1/runs/{rid}")
        assert r.status_code == 200
        body = r.json()
        assert {
            "status",
            "current_agent",
            "agents_done",
            "agents_total",
            "tokens_in",
            "tokens_out",
            "report_ready",
            "status_stale",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        } <= set(body)

    def test_detail_404(self, api_client, dbconn):
        assert api_client.get("/api/v1/runs/r-nope").status_code == 404


class TestArtifacts:
    def test_paths_only(self, api_client, dbconn, settings):
        """R-API-05 / AM-18:只返回路径列表,不含内容。"""
        reports = Path(settings.ta_data_dir) / "logs" / "1810.HK" / "2026-09-14" / "reports"
        reports.mkdir(parents=True)
        (reports / "investment_plan.md").write_text("机密内容", encoding="utf-8")
        settings.smb_prefix = "\\\\nas\\ta"
        rid = seed_run(dbconn, 1, status="succeeded")
        r = api_client.get(f"/api/v1/runs/{rid}/artifacts")
        assert r.status_code == 200
        body = r.json()
        assert body["paths"] == ["\\\\nas\\ta\\1810.HK\\2026-09-14\\reports\\investment_plan.md"]
        assert "机密内容" not in r.text

    def test_empty_and_404(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="failed")
        r = api_client.get(f"/api/v1/runs/{rid}/artifacts")
        assert r.status_code == 200 and r.json()["paths"] == []
        assert api_client.get("/api/v1/runs/r-none/artifacts").status_code == 404


class TestCancelResume:
    def test_cancel_queued(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="queued")
        r = api_client.post(f"/api/v1/runs/{rid}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"

    def test_cancel_running_sets_flag(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="running")
        r = api_client.post(f"/api/v1/runs/{rid}/cancel")
        assert r.status_code == 200
        assert r.json()["cancel_requested_at"] is not None

    def test_cancel_finished_409(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="succeeded")
        r = api_client.post(f"/api/v1/runs/{rid}/cancel")
        assert r.status_code == 409 and r.json()["error"] == "conflict"

    def test_cancel_404(self, api_client, dbconn):
        assert api_client.post("/api/v1/runs/r-x/cancel").status_code == 404

    def test_resume_from_cancelled(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="cancelled", date="2026-09-10", csv="market,social,news", rid="r-c1")
        r = api_client.post(f"/api/v1/runs/{rid}/resume")
        assert r.status_code == 200
        body = r.json()
        assert body["resumed_from"] == "r-c1" and body["status"] == "queued"
        assert body["analysts"] == ["market", "social", "news"]  # 复制快照
        row = dbconn.execute(
            "SELECT actor FROM audit_log WHERE entity='run' AND action='create_run' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["actor"] == "api"

    def test_resume_status_conflict_409(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="succeeded")
        r = api_client.post(f"/api/v1/runs/{rid}/resume")
        assert r.status_code == 409 and r.json()["error"] == "conflict"

    def test_resume_busy_409(self, api_client, dbconn):
        rid = seed_run(dbconn, 1, status="failed", date="2026-09-10", rid="r-f1")
        seed_run(dbconn, 1, status="running", rid="r-b1")
        r = api_client.post(f"/api/v1/runs/{rid}/resume")
        assert r.status_code == 409 and r.json()["error"] == "busy"
        assert "current" in r.json()

    def test_resume_404(self, api_client, dbconn):
        assert api_client.post("/api/v1/runs/r-x/resume").status_code == 404


class TestAuthGate:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/api/v1/runs"),
            ("get", "/api/v1/runs"),
            ("get", "/api/v1/runs/r-x"),
            ("get", "/api/v1/runs/r-x/artifacts"),
            ("post", "/api/v1/runs/r-x/cancel"),
            ("post", "/api/v1/runs/r-x/resume"),
        ],
    )
    def test_all_endpoints_401(self, api_client, method, path):
        api_client.headers.pop("Authorization")
        r = getattr(api_client, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401
