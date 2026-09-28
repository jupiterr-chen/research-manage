"""T-14:ReportsPoller 空闲健康探测 + 页面徽标单元测试。

不发起真实网络请求、不真实睡眠:用假客户端脚本化 `health_ready` 结果,通过
直接改 `_next_probe_at` 模拟间隔到期。
"""

from __future__ import annotations

import pytest

from app import db as app_db
from app.db import connect
from app.reports.client import ConnectionFailed, ProblemError, RequestTimeout
from app.reports.poller import PROBE_TIMEOUT_SECONDS, ReportsPoller
from app.services import report_jobs as rj
from app.web.routes.reports import templates


def _problem(status, code, retryable=False):
    return ProblemError(status, {"code": code, "detail": code, "retryable": retryable}, {})


class ProbeOnlyClient:
    """只实现 health_ready;results 为脚本队列(最后一个可重复)。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[float | None] = []

    def health_ready(self, *, timeout=None):
        self.calls.append(timeout)
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        return item


class JobAndProbeClient(ProbeOnlyClient):
    """任务 + 探测:验证探测异常不阻断任务流程。"""

    def __init__(self, results):
        super().__init__(results)
        self.submit_calls = 0
        self.submit_error: Exception | None = None

    def submit_job(self, symbols, *, idempotency_key, last_n, refresh, forms_by_market=None):
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return 202, {"job_id": "job_h", "status": "queued"}


def _poller(tmp_path, make_settings, client, **settings_overrides):
    s = make_settings(reports_base_url="http://127.0.0.1:1", **settings_overrides)
    s.db_path = str(tmp_path / "probe.db")
    conn = connect(s.db_path)
    app_db.migrate(conn)
    conn.close()
    return s, ReportsPoller(s, lambda: connect(s.db_path), client=client, probe_interval=60.0)


@pytest.fixture
def probe_env(tmp_path, make_settings):
    client = ProbeOnlyClient([{"status": "ok"}])
    s, poller = _poller(tmp_path, make_settings, client)
    return s, client, poller


class TestProbe:
    def test_first_tick_probes_when_idle(self, probe_env):
        s, client, poller = probe_env
        before = poller.health()
        assert before["enabled"] is True and before["ready"] is None
        assert before["reachable"] is None and before["last_probe_at"] is None
        poller.tick_once()  # 无任务也能探测
        h = poller.health()
        assert h["ready"] is True and h["reachable"] is True
        assert h["last_probe_at"] and h["probe_error"] is None
        assert len(client.calls) == 1

    def test_interval_throttles_then_reprobes(self, probe_env):
        s, client, poller = probe_env
        poller.tick_once()
        poller.tick_once()
        poller.tick_once()
        assert len(client.calls) == 1  # 间隔内不重复请求
        poller._next_probe_at = 0.0  # 模拟到期
        poller.tick_once()
        assert len(client.calls) == 2

    def test_connection_failure_then_recovers(self, probe_env):
        s, client, poller = probe_env
        client.results = [ConnectionFailed("refused"), {"status": "ok"}]
        poller.tick_once()
        h = poller.health()
        assert h["reachable"] is False and h["ready"] is False and h["probe_error"]
        poller._next_probe_at = 0.0
        poller.tick_once()
        h2 = poller.health()
        assert h2["reachable"] is True and h2["ready"] is True and h2["probe_error"] is None
        assert h2["last_probe_at"]

    def test_recovers_after_http_error(self, probe_env):
        s, client, poller = probe_env
        client.results = [_problem(503, "store_unavailable"), {"status": "ok"}]
        poller.tick_once()
        assert poller.health()["ready"] is False and poller.health()["reachable"] is True
        poller._next_probe_at = 0.0
        poller.tick_once()
        h = poller.health()
        assert h["ready"] is True and h["reachable"] is True and h["probe_error"] is None
        assert h["last_probe_at"]

    def test_timeout_marks_not_ready(self, probe_env):
        s, client, poller = probe_env
        client.results = [RequestTimeout("GET /health/ready 超时")]
        poller.tick_once()
        h = poller.health()
        assert h["reachable"] is False and h["ready"] is False and "超时" in h["probe_error"]

    @pytest.mark.parametrize(
        ("status", "code"),
        [(401, "unauthorized"), (403, "forbidden"), (503, "store_unavailable")],
    )
    def test_http_error_is_reachable_but_not_ready(self, probe_env, status, code):
        s, client, poller = probe_env
        client.results = [_problem(status, code)]
        poller.tick_once()
        h = poller.health()
        assert h["reachable"] is True  # HTTP 到了服务
        assert h["ready"] is False  # 但没就绪,不得显示正常
        assert str(status) in h["probe_error"]

    def test_200_with_non_ok_status_is_not_ready(self, probe_env):
        s, client, poller = probe_env
        client.results = [{"status": "unavailable"}]
        poller.tick_once()
        h = poller.health()
        assert h["reachable"] is True and h["ready"] is False

    def test_probe_exception_does_not_raise_or_busy_loop(self, probe_env):
        s, client, poller = probe_env
        client.results = [RuntimeError("boom")]
        poller.tick_once()  # 不抛
        h = poller.health()
        assert h["ready"] is False and "boom" in h["probe_error"]
        assert poller._next_probe_at > 0
        poller.tick_once()
        assert len(client.calls) == 1  # 异常后仍节流

    def test_probe_failure_does_not_block_jobs(self, tmp_path, make_settings):
        client = JobAndProbeClient([RuntimeError("probe down")])
        s, poller = _poller(tmp_path, make_settings, client)
        conn = connect(s.db_path)
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        conn.close()
        poller.tick_once()
        conn = connect(s.db_path)
        out = rj.get(conn, job["id"])
        conn.close()
        assert out["status"] == "queued" and client.submit_calls == 1
        assert poller.health()["ready"] is False

    def test_probe_timeout_capped_by_configured_timeout(self, tmp_path, make_settings):
        client = ProbeOnlyClient([{"status": "ok"}])
        s, poller = _poller(tmp_path, make_settings, client, reports_timeout=30.0)
        poller.tick_once()
        assert client.calls == [PROBE_TIMEOUT_SECONDS]

        configured = 1.0
        client2 = ProbeOnlyClient([{"status": "ok"}])
        s2, poller2 = _poller(tmp_path, make_settings, client2, reports_timeout=configured)
        poller2.tick_once()
        assert client2.calls == [configured]

    def test_disabled_never_requests(self, tmp_path, make_settings):
        client = ProbeOnlyClient([{"status": "ok"}])
        s = make_settings()  # 无 REPORTS_API_BASE_URL → 功能关闭
        s.db_path = str(tmp_path / "off.db")
        conn = connect(s.db_path)
        app_db.migrate(conn)
        conn.close()
        poller = ReportsPoller(s, lambda: connect(s.db_path), client=client)
        poller.tick_once()
        assert client.calls == []
        assert poller.health()["enabled"] is False and poller.health()["ready"] is None

    def test_probe_error_and_task_error_do_not_overwrite(self, tmp_path, make_settings):
        client = JobAndProbeClient([RuntimeError("probe bleh")])
        s, poller = _poller(tmp_path, make_settings, client)
        conn = connect(s.db_path)
        rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        conn.close()
        # 任务连接失败 → last_error;探测失败 → probe_error;互不覆盖
        client.submit_error = ConnectionFailed("refused")
        poller.tick_once()
        h = poller.health()
        assert h["probe_error"] and "bleh" in h["probe_error"]
        assert h["last_error"] and "refused" in h["last_error"]


def _render(health):
    return templates.get_template("fragments/report_jobs.html").render(
        reports_health=health, jobs=[], message=None, error=None
    )


class TestPageBadge:
    def test_ready_shows_normal(self):
        html = _render(
            {
                "enabled": True,
                "alive": True,
                "reachable": True,
                "ready": True,
                "active": 0,
                "last_error": None,
                "last_probe_at": "2026-09-28T21:13:00+08:00",
                "probe_error": None,
            }
        )
        assert "服务正常" in html and "最近检查 2026-09-28T21:13:00+08:00" in html
        assert 'class="light ok"' in html
        assert "等待首次自动探测" not in html  # 已探测,不再提示等待

    def test_not_ready_never_says_normal(self):
        html = _render(
            {
                "enabled": True,
                "alive": True,
                "reachable": True,
                "ready": False,
                "active": 0,
                "last_error": None,
                "last_probe_at": "2026-09-28T21:13:00+08:00",
                "probe_error": "HTTP 503 [store_unavailable]",
            }
        )
        assert "服务未就绪" in html and "正常" not in html
        assert "HTTP 503 [store_unavailable]" in html
        assert "等待首次自动探测" not in html  # 已探测(失败),不再提示等待

    def test_unreachable_and_unprobed(self):
        missed = _render(
            {
                "enabled": True,
                "alive": True,
                "reachable": False,
                "ready": False,
                "active": 0,
                "last_error": None,
                "last_probe_at": "2026-09-28T21:13:00+08:00",
                "probe_error": "refused",
            }
        )
        assert "服务不可达" in missed and "等待首次自动探测" not in missed
        unknown = _render(
            {
                "enabled": True,
                "alive": True,
                "reachable": None,
                "ready": None,
                "active": 0,
                "last_error": None,
                "last_probe_at": None,
                "probe_error": None,
            }
        )
        assert "服务未探测" in unknown
        # 未探测状态在 title 解释「等待首次自动探测」
        assert "等待首次自动探测" in unknown

    def test_probe_error_is_escaped(self):
        html = _render(
            {
                "enabled": True,
                "alive": True,
                "reachable": True,
                "ready": False,
                "active": 0,
                "last_error": None,
                "last_probe_at": "2026-09-28T21:13:00+08:00",
                "probe_error": "<script>alert(1)</script>",
            }
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_disabled_hides_badge(self):
        html = _render({"enabled": False, "alive": False})
        assert "服务" not in html.split("<h2>", 1)[1].split("</h2>", 1)[0]
