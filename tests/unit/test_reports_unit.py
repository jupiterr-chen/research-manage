"""财报获取对接单测:符号映射、report_job 服务、轮询线程(假客户端)、配置、页面/API 关闭态。

不依赖 mock 服务;真实 HTTP 契约见 tests/integration/test_reports_mock.py。
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.config import Settings
from app.db import connect
from app.reports import symbols
from app.reports.client import ConnectionFailed, ProblemError, _filename_from_disposition
from app.reports.poller import ReportsPoller
from app.services import instruments as inst_srv
from app.services import report_jobs as rj
from app.services.errors import ConflictError, NotFound, ValidationError
from app.web.routes.reports import _build_filename
from app.web.server import create_app
from tests.conftest import FakeLauncher

# ---------------------------------------------------------------- 符号映射


class TestSymbols:
    @pytest.mark.parametrize(
        "market,code,submit,archive",
        [
            ("cn", "600519.SS", "600519", "600519"),
            ("cn", "000001.SZ", "000001", "000001"),
            ("hk", "1810.HK", "1810.HK", "01810"),
            ("hk", "700.hk", "0700.HK", "00700"),
            ("us", "NVDA", "NVDA", "NVDA"),
        ],
    )
    def test_mapping(self, market, code, submit, archive):
        assert symbols.to_fetcher_symbol(market, code) == submit
        assert symbols.archive_symbol(market, code) == archive

    def test_resolve_infers_market(self):
        assert symbols.resolve("1810.HK") == ("hk", "1810.HK")
        assert symbols.resolve("600519.SS") == ("cn", "600519.SS")
        assert symbols.resolve("nvda") == ("us", "NVDA")
        with pytest.raises(ValueError):
            symbols.resolve("ABC;rm")


# ---------------------------------------------------------------- 服务层


@pytest.fixture
def rconn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    app_db.migrate(c)
    inst_srv.create(c, market="hk", code="1810.HK", name="小米", actor="web")
    yield c
    c.close()


class TestReportJobService:
    def test_create_sets_key_symbol_and_instrument(self, rconn):
        job = rj.create(
            rconn, code="1810.hk", market=None, last_n=None, refresh=False, trigger="web", actor="web"
        )
        assert job["status"] == "pending" and job["symbol"] == "1810.HK" and job["last_n"] == 4
        assert job["idempotency_key"].startswith("am-") and job["idempotency_key"].isascii()
        assert job["instrument_id"] == 1 and job["id"].startswith("rj-")
        audit = rconn.execute("SELECT action, entity FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert (audit["action"], audit["entity"]) == ("create", "report_job")

    def test_create_unknown_instrument_ok_and_validation(self, rconn):
        job = rj.create(rconn, code="NVDA", market=None, last_n=2, refresh=True, trigger="api", actor="api")
        assert job["instrument_id"] is None and job["refresh"] is True
        with pytest.raises(ValidationError):
            rj.create(
                rconn, code="600519.SS", market=None, last_n=21, refresh=False, trigger="web", actor="web"
            )
        with pytest.raises(ValidationError):
            rj.create(
                rconn, code="bad;code", market=None, last_n=1, refresh=False, trigger="web", actor="web"
            )

    def test_active_conflict_per_code(self, rconn):
        rj.create(rconn, code="1810.HK", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        with pytest.raises(ConflictError):
            rj.create(rconn, code="1810.HK", market=None, last_n=2, refresh=False, trigger="web", actor="web")
        # 不同标的可并行
        rj.create(rconn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        assert len(rj.active_jobs(rconn)) == 2

    def test_finalize_partial_keeps_report_ids_and_warnings(self, rconn):
        job = rj.create(
            rconn, code="1810.HK", market=None, last_n=2, refresh=False, trigger="web", actor="web"
        )
        rj.mark_submitted(rconn, job["id"], remote_job_id="job_x", remote_status="queued")
        doc = {
            "status": "partial",
            "progress": {"symbols_total": 1, "symbols_finished": 1},
            "summary": {"downloaded": 1, "cached": 0, "failed": 0},
            "results": [
                {
                    "symbol": "01810",
                    "status": "partial",
                    "report_ids": ["r_1", "r_2"],
                    "items": [],
                    "warnings": ["报告期未知(period_source=unknown)"],
                    "error": None,
                }
            ],
        }
        out = rj.finalize_remote(rconn, job["id"], doc)
        assert out["status"] == "partial" and out["report_ids"] == ["r_1", "r_2"]
        assert out["warnings"] == ["01810: 报告期未知(period_source=unknown)"] and out["error"] is None
        assert out["is_terminal"] and out["remote_job_id"] == "job_x"

    def test_finalize_failed_records_retryable_error(self, rconn):
        job = rj.create(rconn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        rj.mark_submitted(rconn, job["id"], remote_job_id="job_f", remote_status="running")
        doc = {
            "status": "failed",
            "results": [
                {
                    "symbol": "NVDA",
                    "status": "failed",
                    "report_ids": [],
                    "warnings": [],
                    "error": {"code": "source_unavailable", "retryable": True},
                }
            ],
        }
        out = rj.finalize_remote(rconn, job["id"], doc)
        assert out["status"] == "failed" and out["error_code"] == "source_unavailable"
        assert out["error_retryable"] is True and out["report_ids"] == []

    def test_finalize_rejects_non_terminal(self, rconn):
        job = rj.create(rconn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        with pytest.raises(ValueError):
            rj.finalize_remote(rconn, job["id"], {"status": "running"})

    def test_timeout_then_refresh(self, rconn):
        job = rj.create(rconn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        with pytest.raises(ConflictError):
            rj.reopen_for_refresh(rconn, job["id"], actor="web")  # 非 timeout
        rj.mark_submitted(rconn, job["id"], remote_job_id="job_t", remote_status="running")
        rj.finalize_local(rconn, job["id"], status="timeout", error="budget", error_code="wait_timeout")
        again = rj.reopen_for_refresh(rconn, job["id"], actor="web")
        assert again["status"] == "running" and again["error"] is None
        with pytest.raises(NotFound):
            rj.get(rconn, "rj-nope")


# ---------------------------------------------------------------- 轮询线程(假客户端)


class FakeReportsClient:
    """脚本化客户端:submit_script / job_docs 控制返回,记录收到的 Idempotency-Key。"""

    def __init__(self):
        self.submit_calls: list[tuple[str, dict]] = []
        self.submit_script: list = []  # 每次 submit 弹出一个:("ok", job_id) | Exception
        self.job_docs: dict[str, list[dict]] = {}
        self.get_calls = 0

    def submit_job(self, symbols, *, idempotency_key, last_n, refresh, forms_by_market=None):
        self.submit_calls.append(
            (idempotency_key, {"symbols": symbols, "last_n": last_n, "refresh": refresh})
        )
        item = self.submit_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return 202, {"job_id": item[1], "status": "queued"}

    def get_job(self, job_id, *, timeout=None):
        self.get_calls += 1
        docs = self.job_docs[job_id]
        return docs.pop(0) if len(docs) > 1 else docs[0]


def _problem(status, code, retry_after=None, retryable=False):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return ProblemError(status, {"code": code, "detail": code, "retryable": retryable}, headers)


@pytest.fixture
def poller_env(tmp_path, make_settings):
    s = make_settings(reports_base_url="http://127.0.0.1:1", reports_max_wait=5.0, reports_poll_interval=0.0)
    s.db_path = str(tmp_path / "p.db")
    conn = connect(s.db_path)
    app_db.migrate(conn)
    fake = FakeReportsClient()
    poller = ReportsPoller(s, lambda: connect(s.db_path), client=fake)
    return s, conn, fake, poller


class TestPoller:
    def test_submit_then_poll_to_terminal(self, poller_env):
        s, conn, fake, poller = poller_env
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [("ok", "job_1")]
        fake.job_docs["job_1"] = [
            {"status": "queued", "progress": {"symbols_total": 1, "symbols_finished": 0}},
            {"status": "running", "progress": {"symbols_total": 1, "symbols_finished": 0}},
            {
                "status": "succeeded",
                "progress": {"symbols_total": 1, "symbols_finished": 1},
                "summary": {"downloaded": 1},
                "results": [{"symbol": "NVDA", "status": "succeeded", "report_ids": ["r_a"], "warnings": []}],
            },
        ]
        poller.tick_once()  # submit
        assert rj.get(conn, job["id"])["status"] == "queued"
        poller.tick_once()  # queued
        poller.tick_once()  # running
        assert rj.get(conn, job["id"])["status"] == "running"
        poller.tick_once()  # succeeded
        out = rj.get(conn, job["id"])
        assert out["status"] == "succeeded" and out["report_ids"] == ["r_a"]
        assert poller.health()["reachable"] is True and poller.health()["active"] == 0
        assert fake.submit_calls[0][1]["symbols"] == ["NVDA"]

    def test_429_retries_with_same_key_after_retry_after(self, poller_env, monkeypatch):
        s, conn, fake, poller = poller_env
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [_problem(429, "queue_full", retry_after=1), ("ok", "job_2")]
        fake.job_docs["job_2"] = [{"status": "succeeded", "results": []}]
        poller.tick_once()
        after = rj.get(conn, job["id"])
        assert after["status"] == "pending" and after["next_attempt_at"] and after["submit_attempts"] == 1
        poller.tick_once()  # 未到 next_attempt_at,不提交
        assert len(fake.submit_calls) == 1
        # 让时间前进:直接把 next_attempt_at 改到过去
        conn.execute(
            "UPDATE report_job SET next_attempt_at='2000-01-01T00:00:00+08:00' WHERE id=?", (job["id"],)
        )
        conn.commit()
        poller.tick_once()
        assert len(fake.submit_calls) == 2
        assert fake.submit_calls[0][0] == fake.submit_calls[1][0] == job["idempotency_key"]
        poller.tick_once()
        assert rj.get(conn, job["id"])["status"] == "succeeded"

    def test_conflict_and_validation_are_terminal_errors(self, poller_env):
        s, conn, fake, poller = poller_env
        j1 = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        j2 = rj.create(conn, code="MU", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [_problem(409, "idempotency_conflict"), _problem(422, "invalid_request")]
        poller.tick_once()
        codes = {rj.get(conn, j["id"])["error_code"] for j in (j1, j2)}
        assert {rj.get(conn, j["id"])["status"] for j in (j1, j2)} == {"error"}
        assert codes == {"idempotency_conflict", "invalid_request"}

    def test_connection_failure_retries_same_key(self, poller_env):
        s, conn, fake, poller = poller_env
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [ConnectionFailed("refused"), ("ok", "job_3")]
        fake.job_docs["job_3"] = [{"status": "succeeded", "results": []}]
        poller.tick_once()
        assert rj.get(conn, job["id"])["status"] == "pending" and poller.health()["reachable"] is False
        conn.execute("UPDATE report_job SET next_attempt_at=NULL WHERE id=?", (job["id"],))
        conn.commit()
        poller.tick_once()
        assert fake.submit_calls[0][0] == fake.submit_calls[1][0]
        poller.tick_once()
        assert rj.get(conn, job["id"])["status"] == "succeeded"

    def test_bounded_wait_marks_timeout(self, poller_env):
        s, conn, fake, poller = poller_env
        s.reports_max_wait = 0.0  # 预算为 0:提交后第一次轮询即超时
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [("ok", "job_4")]
        fake.job_docs["job_4"] = [{"status": "running"}]
        # 提交阶段预算已耗尽 → timeout
        poller.tick_once()
        out = rj.get(conn, job["id"])
        assert out["status"] == "timeout" and out["error_code"] == "submit_timeout"

    def test_poll_timeout_keeps_remote_job_id_for_refresh(self, poller_env):
        s, conn, fake, poller = poller_env
        job = rj.create(conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web")
        fake.submit_script = [("ok", "job_5")]
        fake.job_docs["job_5"] = [{"status": "running"}, {"status": "succeeded", "results": []}]
        poller.tick_once()
        s.reports_max_wait = 0.0
        poller.tick_once()
        out = rj.get(conn, job["id"])
        assert (
            out["status"] == "timeout"
            and out["error_code"] == "wait_timeout"
            and out["remote_job_id"] == "job_5"
        )
        rj.reopen_for_refresh(conn, job["id"], actor="web")
        s.reports_max_wait = 60.0
        poller.tick_once()  # running
        poller.tick_once()  # succeeded
        assert rj.get(conn, job["id"])["status"] == "succeeded"

    def test_disabled_poller_is_noop(self, make_settings, tmp_path):
        s = make_settings()
        s.db_path = str(tmp_path / "d.db")
        p = ReportsPoller(s, lambda: connect(s.db_path))
        p.start()
        assert not p.is_alive() and p.health()["enabled"] is False
        p.stop()


# ---------------------------------------------------------------- 配置


class TestConfig:
    def test_reports_settings_parse_and_validate(self, tmp_path):
        env = {
            "AM_TA_DATA_DIR": str(tmp_path),
            "AM_TA_DATA_HOST": "x",
            "AM_TA_ENV_HOST": "x",
            "AM_RUNNER_HOST": "x",
            "REPORTS_API_BASE_URL": "http://127.0.0.1:18765/",
            "REPORTS_API_TIMEOUT": "5",
            "REPORTS_API_MAX_WAIT": "30",
            "REPORTS_API_POLL_INTERVAL": "0.5",
            "REPORTS_API_TOKEN": "sekret",
        }
        s = Settings.load(env)
        assert s.reports_enabled and s.reports_timeout == 5.0 and s.reports_poll_interval == 0.5
        summ = s.summary()
        assert "sekret" not in str(summ) and summ["reports_token"] == "(已配置)"
        for bad in (
            {"REPORTS_API_BASE_URL": "127.0.0.1:18765"},
            {"REPORTS_API_BASE_URL": "http://x/api/v1"},
            {"REPORTS_API_MAX_WAIT": "0"},
            {"REPORTS_API_LAST_N_DEFAULT": "21"},
        ):
            with pytest.raises(SystemExit):
                Settings.load({**env, **bad})


# ---------------------------------------------------------------- 关闭态的页面与 API


@pytest.fixture
def disabled_client(settings):
    from pathlib import Path as _P

    _P(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
    from app.executor.worker import Worker

    worker = Worker(settings, launcher=FakeLauncher(), db_factory=lambda: connect(settings.db_path))
    app = create_app(settings, worker=worker)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {settings.token}"})
        yield c


class TestDisabledFeature:
    def test_page_shows_notice_and_api_503(self, disabled_client):
        r = disabled_client.get("/reports")
        assert r.status_code == 200 and "REPORTS_API_BASE_URL" in r.text
        assert "http://" not in r.text.replace("http://", "", 0)  # 页面无外链由 test_web_ui 扫描保证
        r = disabled_client.post("/api/v1/report-jobs", json={"code": "NVDA"})
        assert r.status_code == 503 and r.json()["error"] == "reports_disabled"
        r = disabled_client.get("/api/v1/archive/reports")
        assert r.status_code == 503
        assert disabled_client.get("/healthz").json()["reports"]["enabled"] is False

    def test_unauth(self, settings, disabled_client):
        disabled_client.headers.pop("Authorization")
        assert disabled_client.get("/api/v1/report-jobs").status_code == 401
        assert disabled_client.get("/reports", follow_redirects=False).status_code == 302


class TestReportJobDetailRender:
    def test_detail_renders_results_without_items(self, settings, disabled_client):
        """复核清单 2:results 缺 `items`(或为 None)时详情页仍渲染 200。"""
        conn = connect(settings.db_path)
        try:
            job = rj.create(
                conn, code="NVDA", market=None, last_n=1, refresh=False, trigger="web", actor="web"
            )
            rj.mark_submitted(conn, job["id"], remote_job_id="job_x", remote_status="running")
            rj.finalize_remote(
                conn,
                job["id"],
                {
                    "status": "partial",
                    "progress": {"symbols_total": 2, "symbols_finished": 2},
                    "results": [
                        {"symbol": "NVDA", "status": "partial", "report_ids": ["r_1"], "warnings": []},
                        {
                            "symbol": "MU",
                            "status": "partial",
                            "report_ids": ["r_2"],
                            "items": None,
                            "warnings": [],
                        },
                    ],
                },
            )
            jid = job["id"]
        finally:
            conn.close()
        r = disabled_client.get(f"/reports/jobs/{jid}")
        assert r.status_code == 200
        assert "r_1" in r.text and "r_2" in r.text


# ---------------------------------------------------------------- 下载文件名(T-12)


class TestDispositionFilename:
    def test_only_filename(self):
        assert _filename_from_disposition('attachment; filename="a.pdf"') == "a.pdf"

    def test_only_filename_star(self):
        v = "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf"
        assert _filename_from_disposition(v) == "中文.pdf"

    def test_both_star_before_plain(self):
        v = "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf; filename=\"a.pdf\""
        assert _filename_from_disposition(v) == "中文.pdf"

    def test_both_plain_before_star(self):
        v = "attachment; filename=\"a.pdf\"; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf"
        assert _filename_from_disposition(v) == "中文.pdf"

    def test_broken_star_falls_back_to_plain(self):
        v = "attachment; filename=\"a.pdf\"; filename*=UTF-8''%FF"
        assert _filename_from_disposition(v) == "a.pdf"

    def test_broken_star_only_returns_none(self):
        assert _filename_from_disposition("attachment; filename*=UTF-8''%FF") is None

    def test_unknown_charset_returns_none(self):
        assert _filename_from_disposition("attachment; filename*=X-NOPE''%41") is None

    def test_empty_or_none(self):
        assert _filename_from_disposition(None) is None
        assert _filename_from_disposition("") is None


class TestBuildFilename:
    def test_readable_upstream_used_as_is(self):
        assert _build_filename({"report_id": "r_1"}, "AAPL_10-Q.pdf", "application/pdf") == "AAPL_10-Q.pdf"

    def test_unknown_prefix_assembled_from_metadata(self):
        report = {
            "report_id": "r_1",
            "market": "HK",
            "symbol": "00700",
            "doc_type": "INTERIM",
            "report_period": "2026-06-30",
            "filing_date": "2026-08-14",
        }
        assert (
            _build_filename(report, "unknown__INTERIM__r_1.pdf", "application/pdf")
            == "HK_0700.HK_INTERIM_2026-06-30_r_1.pdf"
        )

    def test_null_period_uses_filing_date(self):
        report = {
            "report_id": "r_2",
            "market": "HK",
            "symbol": "00700",
            "doc_type": "INTERIM",
            "report_period": None,
            "filing_date": "2026-08-14",
        }
        assert _build_filename(report, None, "application/pdf") == "HK_0700.HK_INTERIM_2026-08-14_r_2.pdf"

    def test_no_date_segment_omitted_and_html_ext(self):
        report = {"report_id": "r_3", "market": "US", "symbol": "AAPL", "doc_type": "10-K"}
        assert _build_filename(report, None, "text/html") == "US_AAPL_10-K_r_3.html"

    def test_missing_metadata_falls_back_to_id_and_ext(self):
        assert _build_filename({"report_id": "r_4"}, None, "application/pdf") == "r_4.pdf"
        assert _build_filename(None, None, "application/octet-stream") == "report.bin"

    def test_sanitizes_spaces_and_separators(self):
        name = _build_filename({"report_id": "r_5"}, "my report/2026.pdf", "application/pdf")
        assert name == "my_report_2026.pdf"

    def test_length_capped_at_150(self):
        assert len(_build_filename({"report_id": "r_6"}, "a" * 200 + ".pdf", "application/pdf")) == 150


class _FakeArchiveClient:
    """代理下载单测用:固定下载结果 + 记录 get_report 调用次数。"""

    def __init__(self, *, filename, media_type="application/pdf", report=None, report_error=None):
        content = b"%PDF-1.4 fake"
        digest = hashlib.sha256(content).hexdigest()
        self.result = {
            "status": 200,
            "content": content,
            "sha256": digest,
            "etag": f'"{digest}"',
            "media_type": media_type,
            "filename": filename,
            "content_length": len(content),
        }
        self.report = report
        self.report_error = report_error
        self.get_report_calls = 0

    def download_report_file(self, report_id, *, artifact_id=None, if_none_match=None):
        return self.result

    def get_report(self, report_id):
        self.get_report_calls += 1
        if self.report_error is not None:
            raise self.report_error
        return self.report


class _FakePoller:
    def __init__(self, client):
        self.client = client

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"enabled": True, "alive": True, "reachable": True, "active": 0, "last_error": None}


@contextmanager
def _archive_client(settings, client):
    from pathlib import Path as _P

    from app.executor.worker import Worker

    _P(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
    worker = Worker(settings, launcher=FakeLauncher(), db_factory=lambda: connect(settings.db_path))
    app = create_app(settings, worker=worker, reports_poller=_FakePoller(client))
    with TestClient(app) as tc:
        tc.headers.update({"Authorization": f"Bearer {settings.token}"})
        yield tc


class TestArchiveFileHeaders:
    def test_readable_upstream_forwarded_without_second_call(self, settings):
        client = _FakeArchiveClient(filename="AAPL_10-Q_2026-06-30.pdf")
        with _archive_client(settings, client) as tc:
            r = tc.get("/api/v1/archive/reports/r_1/file")
        assert r.status_code == 200
        cd = r.headers["Content-Disposition"]
        assert 'filename="AAPL_10-Q_2026-06-30.pdf"' in cd
        assert "filename*=UTF-8''AAPL_10-Q_2026-06-30.pdf" in cd
        assert client.get_report_calls == 0

    def test_chinese_upstream_name_gets_rfc5987_and_ascii_fallback(self, settings):
        client = _FakeArchiveClient(filename="中期報告.pdf")
        with _archive_client(settings, client) as tc:
            r = tc.get("/api/v1/archive/reports/r_zh/file")
        cd = r.headers["Content-Disposition"]
        assert 'filename="_.pdf"' in cd
        assert f"filename*=UTF-8''{quote('中期報告.pdf', safe='')}" in cd

    def test_unknown_prefix_assembles_from_metadata(self, settings):
        client = _FakeArchiveClient(
            filename="unknown__INTERIM__r_1.pdf",
            report={
                "report_id": "r_1",
                "market": "HK",
                "symbol": "00700",
                "doc_type": "INTERIM",
                "report_period": "2026-06-30",
                "filing_date": "2026-08-14",
            },
        )
        with _archive_client(settings, client) as tc:
            r = tc.get("/api/v1/archive/reports/r_1/file")
        assert client.get_report_calls == 1
        assert "filename*=UTF-8''HK_0700.HK_INTERIM_2026-06-30_r_1.pdf" in r.headers["Content-Disposition"]

    def test_metadata_failure_still_downloads(self, settings):
        client = _FakeArchiveClient(
            filename="unknown__.pdf",
            report_error=ProblemError(503, {"code": "unavailable", "detail": "down"}),
        )
        with _archive_client(settings, client) as tc:
            r = tc.get("/api/v1/archive/reports/r_9/file")
        assert r.status_code == 200
        assert "filename*=UTF-8''r_9.pdf" in r.headers["Content-Disposition"]
