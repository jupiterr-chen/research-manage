"""Web UI 单测(R-WEB-01~08 / AM-14/15/17)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.scheduler import Scheduler
from app.services import instruments as inst_srv
from app.services import schedules as sch_srv
from app.web.server import create_app

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "app" / "web" / "templates"
STATIC = REPO / "app" / "web" / "static"


class NoopWorker:
    def self_check(self):
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"alive": True, "docker_ok": True, "queue_depth": 0, "current_run_id": None, "last_tick": None}


@pytest.fixture
def ui(settings):
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
def db(settings, ui):
    conn = connect(settings.db_path)
    inst_srv.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")
    yield conn
    conn.close()


def seed_run(
    conn,
    instrument_id,
    *,
    status="queued",
    date="2026-09-14",
    csv="market,social,news",
    rid=None,
    agents_done=0,
    current_agent=None,
    started="2026-09-14T08:01:00+08:00",
):
    rid = rid or f"r-ui-{status}-{instrument_id}-{date}"
    with conn:
        conn.execute(
            "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
            " trigger, agents_total, agents_done, current_agent, created_at, started_at)"
            " VALUES (?,?,?,?,?,'web',11,?,?, '2026-09-14T08:00:00+08:00', ?)",
            (rid, instrument_id, csv, date, status, agents_done, current_agent, started),
        )
    return rid


class TestPages:
    def test_three_pages_200(self, ui, db):
        rid = seed_run(db, 1)
        assert ui.get("/").status_code == 200
        assert ui.get("/instruments").status_code == 200
        assert ui.get(f"/runs/{rid}").status_code == 200

    def test_login_redirects(self, ui, db):
        ui.headers.pop("Authorization")
        for path in ("/", "/instruments", "/runs/r-x", "/fragments/overview"):
            r = ui.get(path, follow_redirects=False)
            assert r.status_code == 302 and "/login" in r.headers["location"], path

    def test_run_detail_404(self, ui, db):
        assert ui.get("/runs/r-none").status_code == 404


class TestOverviewFragment:
    def test_polling_attributes(self, ui, db):
        r = ui.get("/fragments/overview")
        assert r.status_code == 200
        assert 'id="overview-region"' in r.text
        assert 'hx-trigger="every 15s"' in r.text  # AM-15
        assert 'hx-get="/fragments/overview"' in r.text

    def test_page_includes_fragment(self, ui, db):
        r = ui.get("/")
        assert 'id="overview-region"' in r.text and "every 15s" in r.text

    def test_current_task_card(self, ui, db):
        rid = seed_run(db, 1, status="running", agents_done=3, current_agent="News Analyst")
        r = ui.get("/fragments/overview")
        assert "1810.HK" in r.text and "News Analyst" in r.text and rid in r.text

    def test_recent_and_queue(self, ui, db):
        seed_run(db, 1, status="succeeded", rid="r-ok1", date="2026-09-10")
        seed_run(db, 1, status="queued", rid="r-q1", date="2026-09-11")
        r = ui.get("/fragments/overview")
        assert "r-ok1" in r.text and "r-q1" in r.text


class TestManualRun:
    def test_busy_inline_no_redirect(self, ui, db):
        """R-WEB-03 / AM-01:忙时片段内联显示 409 信息,不跳转。"""
        seed_run(db, 1, status="running", rid="r-busy-x", agents_done=2, current_agent="Bear Researcher")
        r = ui.post(
            "/fragments/run",
            data={"code": "1810.HK", "mode": "profile", "profile_id": "1", "date": "2026-09-14"},
        )
        assert r.status_code == 200  # 内联,非 409 跳转
        assert "flash-error" in r.text and "当前忙" in r.text
        assert "Bear Researcher" in r.text and "1810.HK" in r.text
        n = db.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        assert n == 1  # 零副作用

    def test_success_returns_oob_overview(self, ui, db):
        r = ui.post(
            "/fragments/run",
            data={"code": "1810.HK", "mode": "profile", "profile_id": "1", "date": "2026-09-14"},
        )
        assert r.status_code == 200
        assert 'id="run-result"' in r.text and "已入队" in r.text
        assert 'hx-swap-oob="true"' in r.text and 'id="overview-region"' in r.text
        assert db.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1

    def test_invalid_params_inline(self, ui, db):
        r = ui.post("/fragments/run", data={"code": "0700.HK", "mode": "profile", "profile_id": "1"})
        assert r.status_code == 200 and "flash-error" in r.text  # 标的不存在,内联
        r2 = ui.post("/fragments/run", data={"code": "1810.HK", "mode": "analysts", "analysts": "bogus"})
        assert r2.status_code == 200 and "flash-error" in r2.text


class TestInstrumentsPage:
    def test_describe_text_shown(self, ui, db):
        sch_srv.create(db, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")
        r = ui.get("/instruments")
        assert "小米集团:每交易日 08:30 · 技术+舆情+新闻" in r.text

    def test_monday_conflict_hint(self, ui, db):
        """R-WEB-04:周一日频+周频同标的 → 「周一将只跑全量」。"""
        r0 = ui.get("/instruments")
        assert "周一将只跑全量" not in r0.text
        sch_srv.create(db, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")
        sch_srv.create(db, instrument_id=1, profile_id=2, kind="weekly", weekday=1, actor="web")
        r = ui.get("/instruments")
        assert "周一将只跑全量" in r.text

    def test_add_instrument_and_escape(self, ui, db):
        """R-WEB-06:用户输入自动转义,无 |safe。"""
        r = ui.post(
            "/fragments/instruments",
            data={"market": "us", "code": "NVDA", "name": "<script>alert(1)</script>"},
        )
        assert r.status_code == 200
        assert "&lt;script&gt;" in r.text
        assert "<script>alert(1)</script>" not in r.text

    def test_delete_conflict_lists_schedules(self, ui, db):
        sch_srv.create(db, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")
        r = ui.delete("/fragments/instruments/1")
        assert r.status_code == 200
        assert "flash-error" in r.text and "关联调度" in r.text

    def test_delete_ok(self, ui, db):
        r = ui.delete("/fragments/instruments/1")
        assert "已删除" in r.text

    def test_toggle_instrument(self, ui, db):
        r = ui.post("/fragments/instruments/1", data={"enabled": "0"})
        assert ">启用<" in r.text and ">停用<" not in r.text  # 按钮文案翻转

    def test_schedule_toggle_and_describe_refresh(self, ui, db):
        sch_srv.create(db, instrument_id=1, profile_id=1, kind="daily_trading", actor="web")
        r = ui.post("/fragments/schedules/1/toggle")
        assert "已停用" in r.text  # describe() 后缀


class TestRunDetail:
    def test_timeline_order_and_states(self, ui, db):
        rid = seed_run(db, 1, status="running", agents_done=2, current_agent="News Analyst")
        r = ui.get(f"/runs/{rid}")
        assert "Market Analyst" in r.text and "Portfolio Manager" in r.text
        assert r.text.count('class="done"') == 2
        assert 'class="active"' in r.text

    def test_polling_stops_on_terminal(self, ui, db):
        rid_run = seed_run(db, 1, status="running", agents_done=1, current_agent="Sentiment Analyst")
        r1 = ui.get(f"/fragments/runs/{rid_run}")
        assert 'hx-trigger="every 15s"' in r1.text  # 非终态轮询
        rid_done = seed_run(db, 1, status="succeeded", rid="r-ui-done", agents_done=11)
        r2 = ui.get(f"/fragments/runs/{rid_done}")
        assert "every 15s" not in r2.text  # 终态停止

    def test_container_log_path_only(self, ui, db):
        """AM-18:container.log 只显示路径。"""
        rid = seed_run(db, 1, status="failed", rid="r-ui-f1")
        r = ui.get(f"/runs/{rid}")
        assert "container.log" in r.text and r.text.count("container.log") >= 1
        assert "仅路径" in r.text

    def test_cancel_confirm_and_resume_gate(self, ui, db):
        rid_run = seed_run(
            db, 1, status="running", rid="r-ui-run1", agents_done=1, current_agent="Sentiment Analyst"
        )
        r = ui.get(f"/runs/{rid_run}")
        assert "hx-confirm" in r.text and "取消任务" in r.text
        assert "断点续跑" not in r.text  # running 无续跑按钮
        rid_c = seed_run(db, 1, status="cancelled", rid="r-ui-c1", date="2026-09-10")
        r2 = ui.get(f"/runs/{rid_c}")
        assert "断点续跑" in r2.text

    def test_cancel_action_fragment(self, ui, db):
        rid = seed_run(db, 1, status="running", agents_done=1, current_agent="Sentiment Analyst")
        r = ui.post(f"/fragments/runs/{rid}/cancel")
        assert r.status_code == 200 and "已请求取消" in r.text

    def test_resume_action_fragment(self, ui, db):
        rid = seed_run(db, 1, status="cancelled", rid="r-ui-c2", date="2026-09-10")
        r = ui.post(f"/fragments/runs/{rid}/resume")
        assert r.status_code == 200 and "已创建续跑任务" in r.text


class TestNoExternalLinks:
    def test_templates_and_static_no_http_links(self):
        """R-WEB-01/08 / AM-14:模板与静态资源无 http(s):// 外链。"""
        files = list(TEMPLATES.rglob("*.html")) + list(STATIC.glob("*.css"))
        assert files, "模板缺失"
        for f in files:
            text = f.read_text(encoding="utf-8")
            assert "http://" not in text, f"{f} 含外链"
            assert "https://" not in text, f"{f} 含外链"

    def test_no_safe_filter(self):
        for f in TEMPLATES.rglob("*.html"):
            assert "|safe" not in f.read_text(encoding="utf-8"), f"{f} 使用了 |safe"

    def test_htmx_local(self, ui):
        r = ui.get("/")
        assert 'src="/static/htmx.min.js"' in r.text
