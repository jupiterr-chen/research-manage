"""财报获取任务轮询线程:提交(幂等键复用)→ 有界轮询 → 落终态。

与 Worker(docker)线程互不相干;所有对 reports-fetcher 的 HTTP 调用都在本线程或
Web 层的归档代理里,任务状态只写 SQLite(services.report_jobs)。

边界:
- 提交:同一本地任务的所有重试复用同一 Idempotency-Key;429 按 Retry-After 延后;
  409 idempotency_conflict / 4xx 校验错 → error;连不上/超时 → 延后重试。
- 轮询:每任务独立退避(poll_interval → poll_max_interval);单次请求超时不超过剩余预算;
  从 started_at 起超过 max_wait → timeout(服务端任务可能仍在跑,可手动刷新)。
- HTTP 200 不代表成功:只有 status ∈ {succeeded, partial, failed} 才落终态。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from sqlite3 import Connection

from app import models
from app.audit import scrub
from app.config import Settings
from app.reports.client import (
    TERMINAL_STATUSES,
    ConnectionFailed,
    ProblemError,
    ReportsClient,
    RequestTimeout,
)
from app.services import report_jobs

log = logging.getLogger("am.reports")

TICK_SECONDS = 1.0
NON_RETRYABLE_SUBMIT = {
    "idempotency_conflict",
    "invalid_request",
    "missing_idempotency_key",
    "invalid_idempotency_key",
    "unsupported_media_type",
    "payload_too_large",
    "unauthorized",
    "forbidden",
}


def build_client(settings: Settings) -> ReportsClient:
    return ReportsClient(
        settings.reports_base_url,
        settings.reports_token or None,
        timeout=settings.reports_timeout,
        max_wait=settings.reports_max_wait,
        poll_interval=settings.reports_poll_interval,
        poll_max_interval=settings.reports_poll_max_interval,
    )


class ReportsPoller(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        db_factory: Callable[[], Connection],
        client: ReportsClient | None = None,
        *,
        tick: float = TICK_SECONDS,
    ):
        super().__init__(name="reports-poller", daemon=True)
        self.settings = settings
        self.db_factory = db_factory
        self.enabled = settings.reports_enabled
        self.client = client or (build_client(settings) if self.enabled else None)
        self.tick = tick
        self._stop_event = threading.Event()
        self._next_poll: dict[str, tuple[float, float]] = {}  # job_id → (next_at_monotonic, interval)
        self._last_tick: float | None = None
        self._last_error: str | None = None
        self._reachable: bool | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:  # type: ignore[override]
        if not self.enabled:
            log.info("[reports] REPORTS_API_BASE_URL 未配置,财报获取功能关闭")
            return
        super().start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=self.tick * 3 + 2)

    def health(self) -> dict:
        return {
            "enabled": self.enabled,
            "alive": self.is_alive(),
            "reachable": self._reachable,
            "active": len(self._next_poll),
            "last_error": self._last_error,
        }

    def run(self) -> None:
        log.info("[reports] 轮询线程启动 base=%s", self.settings.reports_base_url)
        while not self._stop_event.is_set():
            try:
                self.tick_once()
            except Exception as e:  # noqa: BLE001 - 线程不退出
                self._last_error = scrub(f"{type(e).__name__}: {e}")[:200]
                log.exception("[reports] tick 异常")
            self._last_tick = time.monotonic()
            self._stop_event.wait(self.tick)

    # ---------------------------------------------------------------- 主循环
    def tick_once(self) -> None:
        conn = self.db_factory()
        try:
            jobs = report_jobs.active_jobs(conn)
            seen = set()
            for job in jobs:
                seen.add(job["id"])
                if self._stop_event.is_set():
                    break
                if job["status"] == "pending":
                    self._submit(conn, job)
                else:
                    self._poll(conn, job)
            for stale in set(self._next_poll) - seen:
                self._next_poll.pop(stale, None)
        finally:
            conn.close()

    # ---------------------------------------------------------------- 提交
    def _submit(self, conn, job: dict) -> None:
        nxt = job.get("next_attempt_at")
        if nxt:
            try:
                if datetime.fromisoformat(nxt) > models.now_sh():
                    return
            except ValueError:
                pass
        if self._budget_exhausted(job):
            report_jobs.finalize_local(
                conn, job["id"], status="timeout", error="提交阶段超过总等待上限", error_code="submit_timeout"
            )
            return
        try:
            http_status, accepted = self.client.submit_job(
                [job["symbol"]],
                idempotency_key=job["idempotency_key"],
                last_n=job["last_n"],
                refresh=job["refresh"],
            )
        except ProblemError as e:
            self._reachable = True
            if e.status == 429 or (e.retryable and e.code not in NON_RETRYABLE_SUBMIT):
                delay = (
                    e.retry_after if e.retry_after is not None else self.settings.reports_poll_max_interval
                )
                report_jobs.mark_submit_retry(
                    conn, job["id"], retry_after=delay, reason=f"{e.code}: 稍后重试"
                )
                log.warning(
                    "[reports] job=%s 提交被限流/可重试 code=%s retry_after=%s", job["id"], e.code, delay
                )
                return
            report_jobs.finalize_local(conn, job["id"], status="error", error=str(e), error_code=e.code)
            log.warning("[reports] job=%s 提交失败 code=%s", job["id"], e.code)
            return
        except (ConnectionFailed, RequestTimeout) as e:
            self._reachable = False
            self._last_error = scrub(str(e))[:200]
            report_jobs.mark_submit_retry(
                conn,
                job["id"],
                retry_after=self.settings.reports_poll_interval,
                reason=f"连接失败,同键重试:{e}",
            )
            return
        self._reachable = True
        self._last_error = None
        report_jobs.mark_submitted(
            conn, job["id"], remote_job_id=accepted["job_id"], remote_status=accepted.get("status", "queued")
        )
        self._next_poll[job["id"]] = (time.monotonic(), self.settings.reports_poll_interval)
        log.info("[reports] job=%s 已提交 remote=%s http=%s", job["id"], accepted["job_id"], http_status)

    # ---------------------------------------------------------------- 轮询
    def _poll(self, conn, job: dict) -> None:
        now = time.monotonic()
        next_at, interval = self._next_poll.get(job["id"], (now, self.settings.reports_poll_interval))
        if now < next_at:
            return
        remaining = self._remaining_budget(job)
        if remaining <= 0:
            report_jobs.finalize_local(
                conn,
                job["id"],
                status="timeout",
                error=(
                    f"超过总等待上限 {int(self.settings.reports_max_wait)}s,"
                    f"服务端任务 {job['remote_job_id']} 可能仍在执行"
                ),
                error_code="wait_timeout",
            )
            self._next_poll.pop(job["id"], None)
            return
        try:
            doc = self.client.get_job(
                job["remote_job_id"], timeout=min(self.settings.reports_timeout, remaining)
            )
        except ProblemError as e:
            self._reachable = True
            if e.status == 404:
                report_jobs.finalize_local(conn, job["id"], status="error", error=str(e), error_code=e.code)
                self._next_poll.pop(job["id"], None)
                return
            self._last_error = scrub(str(e))[:200]
            self._next_poll[job["id"]] = (
                now + interval,
                min(interval * 1.5, self.settings.reports_poll_max_interval),
            )
            return
        except (ConnectionFailed, RequestTimeout) as e:
            self._reachable = False
            self._last_error = scrub(str(e))[:200]
            self._next_poll[job["id"]] = (
                now + interval,
                min(interval * 1.5, self.settings.reports_poll_max_interval),
            )
            return
        self._reachable = True
        self._last_error = None
        if doc.get("status") in TERMINAL_STATUSES:
            report_jobs.finalize_remote(conn, job["id"], doc)
            self._next_poll.pop(job["id"], None)
            log.info("[reports] job=%s 终态 %s", job["id"], doc.get("status"))
            return
        report_jobs.update_progress(conn, job["id"], doc)
        self._next_poll[job["id"]] = (
            now + interval,
            min(interval * 1.5, self.settings.reports_poll_max_interval),
        )

    # ---------------------------------------------------------------- 预算
    def _elapsed(self, job: dict) -> float:
        base = job.get("started_at") or job.get("created_at")
        try:
            began = datetime.fromisoformat(base)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, (models.now_sh() - began).total_seconds())

    def _remaining_budget(self, job: dict) -> float:
        return self.settings.reports_max_wait - self._elapsed(job)

    def _budget_exhausted(self, job: dict) -> bool:
        return self._remaining_budget(job) <= 0
