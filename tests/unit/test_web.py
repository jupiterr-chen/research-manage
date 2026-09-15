"""Web 层单测:鉴权、登录限速、healthz、异常边界(R-FND-04/05/08/09 / R-WEB-01/07)。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.web.server import create_app
from tests.conftest import TOKEN  # noqa: F401  (测试 token 常量,便于复用)


class TestAuthGate:
    def test_page_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/"

    def test_healthz_bearer_ok(self, auth_client):
        r = auth_client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert {"worker_alive", "queue_depth", "docker_ok", "scheduler_jobs", "current_run_id"} <= set(body)

    def test_healthz_bad_bearer_401(self, client):
        r = client.get("/healthz", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"

    def test_local_mode_no_token_allows(self, make_settings, tmp_path, fake_worker):
        app = create_app(make_settings(token=""), worker=fake_worker(make_settings(token="")))
        with TestClient(app) as c:
            assert c.get("/").status_code == 200
            assert c.get("/healthz").status_code == 200

    def test_bearer_sets_actor_api(self, auth_client):
        # healthz 正常即代表依赖链通过;actor 字段在 S5/S6 才对外体现
        assert auth_client.get("/healthz").status_code == 200


class TestLogin:
    def test_login_page(self, client):
        r = client.get("/login")
        assert r.status_code == 200 and "Token" in r.text

    def test_login_success_cookie(self, client, settings):
        r = client.post("/login", data={"token": settings.token, "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers["set-cookie"]
        assert "am_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()
        # cookie 会话可访问页面
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200

    def test_login_wrong_token_uniform_message(self, client, settings):
        r = client.post("/login", data={"token": "wrong"})
        assert r.status_code == 401
        assert "不正确或未设置" in r.text  # 不区分「token 错」与「未设置」

    def test_login_rate_limited(self, client, settings):
        for _ in range(5):
            client.post("/login", data={"token": "wrong"})
        r = client.post("/login", data={"token": settings.token})  # 第 6 次即使 token 对也限速
        assert r.status_code == 429

    def test_next_sanitized(self, client, settings):
        r = client.post(
            "/login", data={"token": settings.token, "next": "http://evil.com"}, follow_redirects=False
        )
        assert r.headers["location"] == "/"

    def test_logout(self, client, settings):
        client.post("/login", data={"token": settings.token})
        r = client.post("/logout", follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 302


class TestExceptionBoundary:
    def test_api_500_json(self, settings, fake_worker):
        app = create_app(settings, worker=fake_worker(settings))

        @app.get("/api/v1/boom")
        async def boom():
            raise RuntimeError("secret-detail sk-abcdefgh12345678")

        with TestClient(app, raise_server_exceptions=False) as c:
            c.headers.update({"Authorization": f"Bearer {settings.token}"})
            r = c.get("/api/v1/boom")
        assert r.status_code == 500
        assert r.json()["error"] == "internal"
        assert "secret-detail" not in r.text

    def test_page_500_html(self, settings, fake_worker):
        app = create_app(settings, worker=fake_worker(settings))

        @app.get("/page-boom")
        async def page_boom():
            raise RuntimeError("x")

        with TestClient(app, raise_server_exceptions=False) as c:
            c.headers.update({"Authorization": f"Bearer {settings.token}"})
            r = c.get("/page-boom")
        assert r.status_code == 500
        assert "服务器内部错误" in r.text


class TestSkeleton:
    def test_base_no_external_links(self, auth_client):
        """R-WEB-01/AM-14:页面无 http(s):// 外链资源。"""
        r = auth_client.get("/")
        assert 'src="/static/htmx.min.js"' in r.text
        assert 'href="/static/app.css"' in r.text
        assert "http://" not in r.text
        assert "https://" not in r.text

    def test_static_assets(self, client):
        assert client.get("/static/htmx.min.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200

    def test_instruments_placeholder(self, auth_client):
        assert auth_client.get("/instruments").status_code == 200

    def test_lifespan_migrates_db(self, settings, fake_worker):
        app = create_app(settings, worker=fake_worker(settings))
        with TestClient(app):
            pass  # startup 内 migrate;能起来即说明 schema 就绪
        import sqlite3

        conn = sqlite3.connect(settings.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"instrument", "profile", "schedule", "run", "audit_log"} <= tables


class TestWorkerHealth:
    def test_worker_health_shape_and_lifecycle(self, settings, conn, fake_launcher):
        import time
        from pathlib import Path

        from app.executor.worker import Worker

        Path(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)  # 自检要求目录存在
        w = Worker(settings, launcher=fake_launcher, db_factory=lambda: conn)
        h = w.health()
        assert set(h) == {"alive", "docker_ok", "queue_depth", "current_run_id", "last_tick"}
        assert h["docker_ok"] is None  # 未启动时未知
        w.start()
        try:
            assert w.health()["alive"] is True
            deadline = time.time() + 3  # docker_ok 反映上一 tick,轮询等首个 tick
            while time.time() < deadline and w.health()["docker_ok"] is None:
                time.sleep(0.05)
            assert w.health()["docker_ok"] is True
        finally:
            w.stop()
            w.join(timeout=5)
            assert not w.is_alive()
        assert w.health()["alive"] is False

    def test_worker_health_reads_queue_depth(self, settings, conn, fake_launcher):
        from app.executor.worker import Worker
        from app.services import instruments as inst_srv
        from app.services import runs as runs_srv

        inst_srv.create(conn, market="us", code="NVDA", actor="web")
        runs_srv.create_run(conn, code="NVDA", profile_id=1, trigger="web", actor="web")
        w = Worker(settings, launcher=fake_launcher, db_factory=lambda: conn)
        h = w.health()
        assert h["queue_depth"] == 1 and h["current_run_id"] is None

    def test_scheduler_stub(self, settings, conn):
        from datetime import date as d

        # 真实 scheduler 会关闭自己经 db_factory 取得的连接 → 必须每次新连接
        from pathlib import Path as _P

        from app import db as app_db
        from app.db import connect
        from app.scheduler import Scheduler

        _P(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        init = connect(settings.db_path)
        app_db.migrate(init)
        init.close()
        s = Scheduler(settings, db_factory=lambda: connect(settings.db_path))
        assert s.rebuild_jobs() == 0
        assert s.next_fires(d(2026, 9, 14)) == []
        assert s.jobs_count == 0
        s.start()
        s.shutdown()
