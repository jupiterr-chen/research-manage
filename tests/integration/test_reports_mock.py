"""对**真实 mock HTTP 端点**的集成测试(integration-kit/cases.json 的 test_cases 全覆盖)。

前置:docker compose -f integration-kit/compose.yaml up -d mock(127.0.0.1:18765)。
不可达时整组 skip。`X-Mock-Scenario` 只在本文件通过 `default_headers=` 注入,
业务代码(app.reports / app.web.routes.reports)不含任何 mock 控制项。

层次:
1. ReportsClient 直接对 mock —— 契约行为(幂等、校验、下载校验、304、有界超时)。
2. 全应用(TestClient + 真实 ReportsPoller 线程)对 mock —— 提交→进度→终态→归档→代理下载。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.reports.client import (
    ChecksumMismatch,
    ProblemError,
    ReportsClient,
    WaitTimeout,
)
from app.services import instruments as inst_srv
from app.web.server import create_app
from tests.conftest import FakeLauncher

MOCK = os.environ.get("REPORTS_MOCK_BASE_URL", "http://127.0.0.1:18765")
SCENARIO_HEADER = "X-Mock-Scenario"  # 仅测试使用
pytestmark = pytest.mark.reports_mock


def _mock_up() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(MOCK + "/health/ready", timeout=3) as r:  # noqa: S310
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


if not _mock_up():
    pytest.skip(
        f"reports-fetcher mock 不可达:{MOCK}(先 docker compose -f integration-kit/compose.yaml up -d mock)",
        allow_module_level=True,
    )


def _client(scenario: str | None = None, **kw) -> ReportsClient:
    headers = {SCENARIO_HEADER: scenario} if scenario else None
    return ReportsClient(
        MOCK, timeout=5, max_wait=30, poll_interval=0.2, poll_max_interval=1, default_headers=headers, **kw
    )


def _key() -> str:
    return f"am-test-{uuid.uuid4().hex}"


# ============================================================ 1. 客户端契约


class TestClientContract:
    def test_health(self):
        c = _client()
        assert c.health_live()["status"] == "ok" and c.health_ready()["status"] == "ok"

    def test_happy_path_submit_poll_list_detail_download_checksum(self):
        c = _client()
        status, acc = c.submit_job(["AAPL"], idempotency_key=_key(), last_n=2)
        assert status == 202 and acc["status"] == "queued" and acc["job_id"]
        seen = []
        doc = c.wait_for_terminal(acc["job_id"], on_poll=lambda d: seen.append(d["status"]))
        assert doc["status"] == "succeeded" and seen[-1] == "succeeded"
        assert doc["progress"]["symbols_finished"] == 1
        res = doc["results"][0]
        assert res["status"] == "succeeded" and len(res["report_ids"]) == 2
        assert set(res["coverage"]) >= {"requested", "selected", "exhausted", "truncated", "notices"}
        page = c.list_reports(market="US", symbol="AAPL", limit=5)
        assert {i["report_id"] for i in page["items"]} >= set(res["report_ids"])
        rid = res["report_ids"][0]
        detail = c.get_report(rid)
        expected = next(a["sha256"] for a in detail["artifacts"] if a["is_current"])
        blob = c.download_report_file(rid, expected_sha256=expected)
        assert blob["status"] == 200 and blob["sha256"] == expected
        assert blob["etag"] == f'"{expected}"' and blob["media_type"].startswith("text/html")
        assert hashlib.sha256(blob["content"]).hexdigest() == expected
        # 条件下载 → 304 无 body
        again = c.download_report_file(rid, if_none_match=blob["etag"])
        assert again["status"] == 304 and again["content"] is None and again["etag"] == blob["etag"]
        # 校验失败要抛
        with pytest.raises(ChecksumMismatch):
            c.download_report_file(rid, expected_sha256="0" * 64)

    def test_progression_queued_running_terminal(self):
        c = _client("slow")
        _, acc = c.submit_job(["MSFT"], idempotency_key=_key(), last_n=1)
        seen: list[str] = []
        c.wait_for_terminal(acc["job_id"], on_poll=lambda d: seen.append(d["status"]))
        order = [s for i, s in enumerate(seen) if i == 0 or s != seen[i - 1]]
        assert order[-1] in ("succeeded", "partial", "failed") and order[0] in ("queued", "running")
        assert "queued" in seen or "running" in seen

    def test_idempotent_replay_same_job_pending_202_then_terminal_200(self):
        c = _client("slow")
        key = _key()
        s1, a1 = c.submit_job(["AAPL"], idempotency_key=key, last_n=1)
        s2, a2 = c.submit_job(["AAPL"], idempotency_key=key, last_n=1)
        assert (s1, s2) == (202, 202) and a1["job_id"] == a2["job_id"]
        c.wait_for_terminal(a1["job_id"])
        s3, a3 = c.submit_job(["AAPL"], idempotency_key=key, last_n=1)
        assert s3 == 200 and a3["job_id"] == a1["job_id"]
        # 有效默认值:省略 last_n/refresh 与显式默认视为同一请求
        key2 = _key()
        _, b1 = c.submit_job(["AAPL"], idempotency_key=key2)
        _, b2 = c.submit_job(["AAPL"], idempotency_key=key2, last_n=4, refresh=False)
        assert b1["job_id"] == b2["job_id"]

    def test_idempotency_conflict_409(self):
        c = _client()
        key = _key()
        c.submit_job(["AAPL"], idempotency_key=key, last_n=1)
        with pytest.raises(ProblemError) as ei:
            c.submit_job(["AAPL"], idempotency_key=key, last_n=2)
        assert ei.value.status == 409 and ei.value.code == "idempotency_conflict"

    def test_validation_errors_problem_json(self):
        c = _client()
        with pytest.raises(ValueError):
            c.submit_job(["AAPL"], idempotency_key="")  # 客户端侧拒绝空 key
        with pytest.raises(ValueError):
            c.submit_job(["AAPL"], idempotency_key="k" * 129)
        with pytest.raises(ValueError):
            c.submit_job([], idempotency_key=_key())
        with pytest.raises(ProblemError) as e1:
            c.submit_job(["AAPL"], idempotency_key=_key(), last_n=21)
        assert e1.value.status == 422 and e1.value.code == "invalid_request" and not e1.value.retryable
        # 未知字段 / 空 symbols / 非 JSON / 非法 JSON:走底层 _request 构造异常请求
        st, hd, raw = c._request(
            "POST",
            "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": _key()},
            body={"symbols": ["AAPL"], "bogus": 1},
        )
        assert st == 422 and json.loads(raw)["code"] == "invalid_request"
        st, hd, raw = c._request(
            "POST", "/api/v1/fetch-jobs", headers={"Idempotency-Key": _key()}, body={"symbols": []}
        )
        assert st == 422
        st, hd, raw = c._request("POST", "/api/v1/fetch-jobs", body={"symbols": ["AAPL"]})
        assert st == 400 and json.loads(raw)["code"] == "missing_idempotency_key"
        st, hd, raw = c._request(
            "POST", "/api/v1/fetch-jobs", headers={"Idempotency-Key": "k" * 129}, body={"symbols": ["AAPL"]}
        )
        assert st == 400 and json.loads(raw)["code"] == "invalid_idempotency_key"
        req = urllib.request.Request(
            MOCK + "/api/v1/fetch-jobs",
            data=b"symbols=AAPL",
            method="POST",  # noqa: S310
            headers={"Idempotency-Key": _key(), "Content-Type": "text/plain"},
        )
        st, hd, raw = _raw(req)
        assert st == 415 and json.loads(raw)["code"] == "unsupported_media_type"
        req = urllib.request.Request(
            MOCK + "/api/v1/fetch-jobs",
            data=b"{not json",
            method="POST",  # noqa: S310
            headers={"Idempotency-Key": _key(), "Content-Type": "application/json"},
        )
        st, hd, raw = _raw(req)
        assert st == 422 and json.loads(raw)["code"] == "invalid_request"

    def test_queue_full_429_retry_after(self):
        c = _client("queue_full")
        with pytest.raises(ProblemError) as ei:
            c.submit_job(["AAPL"], idempotency_key=_key(), last_n=1)
        e = ei.value
        assert e.status == 429 and e.code == "queue_full" and e.retry_after and e.retry_after > 0

    def test_partial_keeps_usable_files_and_unknown_period(self):
        c = _client("partial")
        _, acc = c.submit_job(["0700.HK"], idempotency_key=_key(), last_n=2)
        doc = c.wait_for_terminal(acc["job_id"])
        assert doc["status"] == "partial"
        res = doc["results"][0]
        assert res["status"] == "partial" and res["report_ids"] and res["warnings"]
        unknown = [c.get_report(r) for r in res["report_ids"]]
        assert any(d["report_period"] is None and d["period_source"] == "unknown" for d in unknown)
        for rid in res["report_ids"]:  # 可用文件仍可下载并校验
            d = c.get_report(rid)
            sha = next(a["sha256"] for a in d["artifacts"] if a["is_current"])
            assert c.download_report_file(rid, expected_sha256=sha)["status"] == 200

    def test_failed_job_is_http_200_with_retryable_error(self):
        c = _client("failed")
        _, acc = c.submit_job(["600519"], idempotency_key=_key(), last_n=1)
        doc = c.wait_for_terminal(acc["job_id"])
        st, _, raw = c._request("GET", f"/api/v1/fetch-jobs/{acc['job_id']}")
        assert st == 200 and doc["status"] == "failed"
        res = doc["results"][0]
        assert res["status"] == "failed" and res["report_ids"] == []
        assert res["error"]["code"] == "source_unavailable" and res["error"]["retryable"] is True

    def test_no_reports_is_success_with_empty_result(self):
        c = _client("no_reports")
        _, acc = c.submit_job(["AAPL"], idempotency_key=_key(), last_n=1)
        doc = c.wait_for_terminal(acc["job_id"])
        res = doc["results"][0]
        assert doc["status"] == "succeeded" and res["status"] == "no_reports" and res["report_ids"] == []
        assert any("no_matching_reports" in w for w in res["warnings"])

    def test_unknown_ids_404(self):
        c = _client()
        for fn in (
            lambda: c.get_job("job_nope"),
            lambda: c.get_report("r_nope"),
            lambda: c.download_report_file("r_nope"),
        ):
            with pytest.raises(ProblemError) as ei:
                fn()
            assert ei.value.status == 404 and ei.value.code == "not_found"
        # artifact 不属于该报告
        _, acc = c.submit_job(["AAPL"], idempotency_key=_key(), last_n=1)
        rid = c.wait_for_terminal(acc["job_id"])["results"][0]["report_ids"][0]
        with pytest.raises(ProblemError) as ei:
            c.download_report_file(rid, artifact_id="a_nope")
        assert ei.value.status == 404

    def test_bounded_timeout_on_slow(self):
        c = _client("slow")
        _, acc = c.submit_job(["AAPL"], idempotency_key=_key(), last_n=1)
        t0 = time.monotonic()
        with pytest.raises(WaitTimeout) as ei:
            c.wait_for_terminal(acc["job_id"], max_wait=1.5)
        assert time.monotonic() - t0 < 4.0
        assert ei.value.job_id == acc["job_id"] and ei.value.last_document["status"] in ("queued", "running")

    def test_pagination_cursor_no_duplicates(self):
        c = _client()
        _, acc = c.submit_job(["TSLA"], idempotency_key=_key(), last_n=5)
        c.wait_for_terminal(acc["job_id"])
        seen, cursor, pages = [], None, 0
        while True:
            page = c.list_reports(market="US", symbol="TSLA", limit=2, cursor=cursor)
            seen += [i["report_id"] for i in page["items"]]
            pages += 1
            cursor = page.get("next_cursor")
            if not cursor or pages > 10:
                break
        assert len(seen) == len(set(seen)) >= 5 and pages >= 3
        with pytest.raises(ProblemError) as ei:
            c.list_reports(cursor="garbage")
        assert ei.value.status == 400 and ei.value.code == "invalid_cursor"

    def test_no_mock_headers_in_production_client(self):
        c = ReportsClient(MOCK)
        assert SCENARIO_HEADER not in c.default_headers
        # 只检查代码中的字符串常量(docstring/注释里的说明性提及不算)
        import ast

        offenders = []
        for path in Path("app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                    val = getattr(body[0], "value", None)
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        docstrings.add(id(val))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    if "X-Mock-Scenario" in node.value or "/__mock/" in node.value:
                        offenders.append(str(path))
        assert offenders == [], f"业务代码含 mock 控制项:{offenders}"


def _raw(req):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# ============================================================ 2. 全应用对 mock


@pytest.fixture
def app_env(tmp_path, make_settings):
    """真实 ReportsPoller 线程 + FakeLauncher 的 Worker;scenario 由 monkeypatch 注入 client 头。"""
    from app.executor.worker import Worker
    from app.reports.poller import ReportsPoller

    s = make_settings(
        reports_base_url=MOCK,
        reports_timeout=5,
        reports_max_wait=30,
        reports_poll_interval=0.2,
        reports_poll_max_interval=1,
    )
    s.db_path = str(tmp_path / "app.db")
    Path(s.ta_data_dir).mkdir(parents=True, exist_ok=True)
    dbf = lambda: connect(s.db_path)  # noqa: E731

    def build(scenario=None):
        worker = Worker(s, launcher=FakeLauncher(), db_factory=dbf)  # 每个 app 一个线程实例
        poller = ReportsPoller(s, dbf, client=_client(scenario), tick=0.2)
        return create_app(s, worker=worker, reports_poller=poller)

    return s, build


def _wait_job(tc: TestClient, job_id: str, timeout=30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = tc.get(f"/api/v1/report-jobs/{job_id}").json()
        if j["status"] in ("succeeded", "partial", "failed", "timeout", "error"):
            return j
        time.sleep(0.2)
    raise AssertionError("本地任务未在期限内到终态")


class TestAppEndToEnd:
    def test_submit_progress_terminal_archive_download(self, app_env):
        s, build = app_env
        with TestClient(build()) as tc:
            tc.headers.update({"Authorization": f"Bearer {s.token}"})
            conn = connect(s.db_path)
            inst_srv.create(conn, market="hk", code="0700.HK", name="腾讯", actor="web")
            conn.close()
            r = tc.post("/api/v1/report-jobs", json={"code": "0700.hk", "last_n": 2})
            assert r.status_code == 202
            job = r.json()
            assert job["status"] == "pending" and job["symbol"] == "0700.HK" and job["instrument_id"] == 1
            # 同标的活动任务 → 409
            assert tc.post("/api/v1/report-jobs", json={"code": "0700.HK"}).status_code == 409
            done = _wait_job(tc, job["id"])
            assert done["status"] == "succeeded" and done["remote_job_id"] and len(done["report_ids"]) == 2
            assert done["submit_attempts"] == 1 and done["progress"]["symbols_finished"] == 1
            # 页面
            page = tc.get(f"/reports/jobs/{job['id']}")
            assert page.status_code == 200 and "已完成" in page.text and "下载原文" in page.text
            lst = tc.get("/reports")
            assert lst.status_code == 200 and job["id"] in lst.text and 'hx-trigger="every 15s"' in lst.text
            # 归档:按本系统代码换算 symbol
            arc = tc.get("/api/v1/archive/reports", params={"code": "0700.HK"}).json()
            assert {i["report_id"] for i in arc["items"]} >= set(done["report_ids"])
            frag = tc.get("/fragments/reports/archive", params={"code": "0700.HK"})
            assert frag.status_code == 200 and done["report_ids"][0] in frag.text
            # 代理下载 + 校验头 + 浏览器 304
            rid = done["report_ids"][0]
            detail = tc.get(f"/api/v1/archive/reports/{rid}").json()
            sha = next(a["sha256"] for a in detail["artifacts"] if a["is_current"])
            f = tc.get(f"/api/v1/archive/reports/{rid}/file")
            assert f.status_code == 200 and f.headers["X-Checksum-SHA256"] == sha
            assert f.headers["ETag"] == f'"{sha}"' and "attachment" in f.headers["Content-Disposition"]
            cd = f.headers["Content-Disposition"]
            assert "filename*=UTF-8''" in cd
            assert unquote(cd.split("filename*=UTF-8''", 1)[1]).endswith(".pdf")
            assert hashlib.sha256(f.content).hexdigest() == sha
            assert f.headers["Content-Type"].startswith("application/pdf")
            f304 = tc.get(f"/api/v1/archive/reports/{rid}/file", headers={"If-None-Match": f.headers["ETag"]})
            assert f304.status_code == 304 and f304.content == b""
            # 未知报告 → 404 透传
            assert tc.get("/api/v1/archive/reports/r_nope").status_code == 404
            assert tc.get("/api/v1/archive/reports/r_nope/file").status_code == 404
            # 审计
            conn = connect(s.db_path)
            acts = [
                r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE entity='report_job'")
            ]
            conn.close()
            assert acts == ["create", "finalize"]
            assert tc.get("/healthz").json()["reports"]["reachable"] is True

    def test_partial_and_failed_and_no_reports_via_app(self, app_env):
        s, build = app_env
        for scenario, code, expect_status, expect_reports in (
            ("partial", "NVDA", "partial", True),
            ("failed", "600519.SS", "failed", False),
            ("no_reports", "MU", "succeeded", False),
        ):
            with TestClient(build(scenario)) as tc:
                tc.headers.update({"Authorization": f"Bearer {s.token}"})
                r = tc.post("/api/v1/report-jobs", json={"code": code, "last_n": 1})
                assert r.status_code == 202
                done = _wait_job(tc, r.json()["id"])
                assert done["status"] == expect_status, (scenario, done)
                assert bool(done["report_ids"]) is expect_reports
                if scenario == "partial":
                    assert done["warnings"] and done["error"] is None
                    page = tc.get(f"/reports/jobs/{done['id']}")
                    assert "部分完成" in page.text and "下载原文" in page.text
                if scenario == "failed":
                    assert done["error_code"] == "source_unavailable" and done["error_retryable"] is True
                if scenario == "no_reports":
                    assert done["results"][0]["status"] == "no_reports"

    def test_queue_full_then_recovers_with_same_key(self, app_env):
        s, build = app_env
        app = build("queue_full")
        with TestClient(app) as tc:
            tc.headers.update({"Authorization": f"Bearer {s.token}"})
            r = tc.post("/api/v1/report-jobs", json={"code": "GOOGL", "last_n": 1})
            jid = r.json()["id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                j = tc.get(f"/api/v1/report-jobs/{jid}").json()
                if j["submit_attempts"] >= 1:
                    break
                time.sleep(0.1)
            assert (
                j["status"] == "pending" and j["submit_attempts"] >= 1 and j["error"].startswith("queue_full")
            )
            conn = connect(s.db_path)
            key_before = conn.execute("SELECT idempotency_key FROM report_job WHERE id=?", (jid,)).fetchone()[
                0
            ]
            conn.close()
            # 切换服务端行为(mock 场景按提交时快照;这里换成正常 client 模拟"限流解除")
            app.state.reports_poller.client = _client(None)
            conn = connect(s.db_path)
            conn.execute("UPDATE report_job SET next_attempt_at=NULL WHERE id=?", (jid,))
            conn.commit()
            conn.close()
            done = _wait_job(tc, jid)
            assert done["status"] == "succeeded" and done["submit_attempts"] >= 2
            conn = connect(s.db_path)
            key_after = conn.execute("SELECT idempotency_key FROM report_job WHERE id=?", (jid,)).fetchone()[
                0
            ]
            conn.close()
            assert key_before == key_after

    def test_bounded_timeout_then_refresh(self, app_env):
        s, build = app_env
        s.reports_max_wait = 1.5
        with TestClient(build("slow")) as tc:
            tc.headers.update({"Authorization": f"Bearer {s.token}"})
            r = tc.post("/api/v1/report-jobs", json={"code": "AMD", "last_n": 1})
            jid = r.json()["id"]
            t0 = time.monotonic()
            done = _wait_job(tc, jid, timeout=10)
            assert (
                done["status"] == "timeout" and done["error_code"] == "wait_timeout" and done["remote_job_id"]
            )
            assert time.monotonic() - t0 < 6
            page = tc.get(f"/reports/jobs/{jid}")
            assert "刷新服务端状态" in page.text
            s.reports_max_wait = 30
            assert tc.post(f"/api/v1/report-jobs/{jid}/refresh").json()["status"] == "running"
            assert _wait_job(tc, jid)["status"] == "succeeded"

    def test_invalid_params_400(self, app_env):
        s, build = app_env
        with TestClient(build()) as tc:
            tc.headers.update({"Authorization": f"Bearer {s.token}"})
            assert tc.post("/api/v1/report-jobs", json={"code": "ABC;rm"}).status_code == 400
            assert tc.post("/api/v1/report-jobs", json={"code": "NVDA", "last_n": 21}).status_code == 400
            assert tc.post("/api/v1/report-jobs", json={"code": "NVDA", "last_n": 0}).status_code == 400
            assert tc.get("/api/v1/archive/reports", params={"code": "bad;"}).status_code == 400
            assert tc.get("/api/v1/report-jobs/rj-nope").status_code == 404
            frag = tc.post("/fragments/reports/jobs", data={"code": "bad;code", "last_n": "1"})
            assert frag.status_code == 200 and "flash-error" in frag.text
