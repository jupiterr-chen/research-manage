"""reports-fetcher v1.0.1 HTTP 客户端(仅标准库;integration-kit/API.md 契约)。

规则(与 integration-kit/AGENT_INSTRUCTIONS.md 对齐):
- 配置只有 base_url(不含 /api/v1)与可选 token;请求超时、总等待、轮询间隔可配。
- 提交任务必带 `Idempotency-Key`,由调用方传入并在网络重试时**复用**;本类不自造 key。
- HTTP 200 不等于业务成功:`get_job` 只返回文档,终态由调用方读 `status` 判断。
- 错误统一抛 `ProblemError`(application/problem+json),429 带 `retry_after`。
- 本模块**没有**任何 mock 概念:不知道 `X-Mock-Scenario`,不调用 `/__mock/*`。
  测试需要场景头时通过 `default_headers=` 注入,生产配置不会设置它。
- 出站请求忽略系统代理(服务在内网/隧道回环)。
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

TERMINAL_STATUSES = ("succeeded", "partial", "failed")
ACTIVE_STATUSES = ("queued", "running")
API_PREFIX = "/api/v1"


class ReportsClientError(Exception):
    """客户端错误基类。"""


class ConnectionFailed(ReportsClientError):
    """连不上服务(DNS/拒绝/TLS)。"""


class RequestTimeout(ReportsClientError):
    """单次请求传输超时。"""


class WaitTimeout(ReportsClientError):
    """轮询总预算耗尽,任务仍未终态。"""

    def __init__(self, job_id: str, last_document: dict | None = None):
        self.job_id = job_id
        self.last_document = last_document
        last = (last_document or {}).get("status", "unknown")
        super().__init__(f"job {job_id} 在预算内未到终态(最后状态 {last})")


class ProblemError(ReportsClientError):
    """服务端返回 application/problem+json。"""

    def __init__(self, status: int, problem: dict, headers=None):
        self.status = status
        self.problem = problem or {}
        self.headers = headers
        self.code = self.problem.get("code") or f"http_{status}"
        self.retryable = bool(self.problem.get("retryable"))
        self.request_id = self.problem.get("request_id")
        ra = headers.get("Retry-After") if headers is not None else None
        self.retry_after: float | None = None
        if ra is not None:
            try:
                self.retry_after = float(ra)
            except ValueError:
                self.retry_after = None
        detail = self.problem.get("detail") or self.problem.get("title") or ""
        super().__init__(f"HTTP {status} [{self.code}] {detail}")


class ChecksumMismatch(ReportsClientError):
    """下载字节的 sha256 与元数据不一致。"""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReportsClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        max_wait: float = 900.0,
        poll_interval: float = 2.0,
        poll_max_interval: float = 10.0,
        default_headers: dict[str, str] | None = None,
        user_agent: str = "agents-manage-reports/1.0",
    ):
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ValueError("REPORTS_API_BASE_URL 必须是 http(s):// 地址")
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.timeout = float(timeout)
        self.max_wait = float(max_wait)
        self.poll_interval = float(poll_interval)
        self.poll_max_interval = float(poll_max_interval)
        self.default_headers = dict(default_headers or {})
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ 低层
    def _request(self, method: str, path: str, *, headers=None, body=None, timeout=None):
        url = self.base_url + path
        merged = {"User-Agent": self.user_agent, "Accept": "application/json"}
        merged.update(self.default_headers)
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        if headers:
            merged.update(headers)
        req = urllib.request.Request(url, data=data, headers=merged, method=method)  # noqa: S310 - base 已限定 http(s)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise RequestTimeout(f"{method} {path} 超时") from exc
            raise ConnectionFailed(str(reason)) from exc
        except TimeoutError as exc:
            raise RequestTimeout(f"{method} {path} 超时") from exc

    @staticmethod
    def _raise_problem(status, headers, body):
        try:
            problem = json.loads(body.decode("utf-8"))
            if not isinstance(problem, dict):
                problem = {"detail": str(problem)[:500]}
        except Exception:  # noqa: BLE001 - 非 JSON 错误体也要能报出来
            problem = {"detail": body.decode("utf-8", "replace")[:500]}
        raise ProblemError(status, problem, headers)

    def _json(self, method, path, *, headers=None, body=None, timeout=None):
        status, resp_headers, raw = self._request(method, path, headers=headers, body=body, timeout=timeout)
        if status >= 400:
            self._raise_problem(status, resp_headers, raw)
        if not raw:
            return status, None
        return status, json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ 健康
    def health_live(self) -> dict:
        return self._json("GET", "/health/live")[1]

    def health_ready(self) -> dict:
        return self._json("GET", "/health/ready")[1]

    # ------------------------------------------------------------------ 任务
    def submit_job(
        self,
        symbols: list[str],
        *,
        idempotency_key: str,
        last_n: int = 4,
        forms_by_market: dict | None = None,
        refresh: bool = False,
    ) -> tuple[int, dict]:
        """POST /api/v1/fetch-jobs。返回 (http_status, JobAccepted):202 新建/未终态重放,200 终态重放。

        `idempotency_key` 必填:同一业务动作的所有重试必须传同一个 key。
        """
        if not symbols:
            raise ValueError("symbols 不能为空")
        if not idempotency_key or not (1 <= len(idempotency_key) <= 128) or not idempotency_key.isascii():
            raise ValueError("Idempotency-Key 须为 1–128 个 ASCII 字符")
        payload: dict = {"symbols": list(symbols), "last_n": int(last_n), "refresh": bool(refresh)}
        if forms_by_market is not None:
            payload["forms_by_market"] = forms_by_market
        status, doc = self._json(
            "POST", f"{API_PREFIX}/fetch-jobs", headers={"Idempotency-Key": idempotency_key}, body=payload
        )
        return status, doc

    def get_job(self, job_id: str, *, timeout: float | None = None) -> dict:
        return self._json("GET", f"{API_PREFIX}/fetch-jobs/{urllib.parse.quote(job_id)}", timeout=timeout)[1]

    def wait_for_terminal(
        self,
        job_id: str,
        *,
        max_wait: float | None = None,
        on_poll: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        """有界轮询:总预算 `max_wait`;单次请求超时被限制在剩余预算内;
        退避从 poll_interval 逐步到 poll_max_interval;读到 succeeded/partial/failed 即停。"""
        budget = self.max_wait if max_wait is None else float(max_wait)
        deadline = time.monotonic() + budget
        interval = self.poll_interval
        last: dict | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(job_id, last)
            try:
                last = self.get_job(job_id, timeout=min(self.timeout, remaining))
            except RequestTimeout as exc:
                raise WaitTimeout(job_id, last) from exc
            if on_poll is not None:
                on_poll(last)
            if last.get("status") in TERMINAL_STATUSES:
                return last
            if should_stop is not None and should_stop():
                raise WaitTimeout(job_id, last)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(job_id, last)
            time.sleep(min(interval, remaining))
            interval = min(interval * 1.5, self.poll_max_interval)

    # ------------------------------------------------------------------ 归档
    def list_reports(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        doc_type: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        params = {
            k: v
            for k, v in (
                ("market", market),
                ("symbol", symbol),
                ("doc_type", doc_type),
                ("period_from", period_from),
                ("period_to", period_to),
                ("limit", limit),
                ("cursor", cursor),
            )
            if v is not None and v != ""
        }
        query = urllib.parse.urlencode(params)
        return self._json("GET", f"{API_PREFIX}/reports" + (f"?{query}" if query else ""))[1]

    def get_report(self, report_id: str) -> dict:
        return self._json("GET", f"{API_PREFIX}/reports/{urllib.parse.quote(report_id)}")[1]

    def download_report_file(
        self,
        report_id: str,
        *,
        artifact_id: str | None = None,
        if_none_match: str | None = None,
        expected_sha256: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """GET .../file。返回 dict:status(200/304)、content(bytes|None)、sha256、etag、media_type、
        filename、content_length。给出 expected_sha256 时不一致抛 ChecksumMismatch。"""
        path = f"{API_PREFIX}/reports/{urllib.parse.quote(report_id)}/file"
        if artifact_id:
            path += "?" + urllib.parse.urlencode({"artifact_id": artifact_id})
        headers = {"Accept": "*/*"}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        status, resp_headers, raw = self._request("GET", path, headers=headers, timeout=timeout)
        if status == 304:
            return {"status": 304, "content": None, "etag": resp_headers.get("ETag"), "sha256": None}
        if status >= 400:
            self._raise_problem(status, resp_headers, raw)
        digest = sha256_hex(raw)
        etag = resp_headers.get("ETag")
        if expected_sha256 and digest != expected_sha256.lower():
            raise ChecksumMismatch(f"sha256 不一致:期望 {expected_sha256[:12]}…,实际 {digest[:12]}…")
        if etag and etag.strip('"').lower() != digest:
            raise ChecksumMismatch(f"ETag 与内容不一致:{etag}")
        return {
            "status": status,
            "content": raw,
            "sha256": digest,
            "etag": etag,
            "media_type": resp_headers.get("Content-Type"),
            "filename": _filename_from_disposition(resp_headers.get("Content-Disposition")),
            "content_length": int(resp_headers.get("Content-Length") or len(raw)),
        }


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            enc = part.split("=", 1)[1]
            if "''" in enc:
                return urllib.parse.unquote(enc.split("''", 1)[1])
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return None
