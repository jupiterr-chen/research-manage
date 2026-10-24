"""每日财报增量同步(DESIGN §4.13 / REPORTS-FETCHER.md §9)。

由 Scheduler 的 am-reports-sync cron 触发;只创建本地 report_job(trigger=schedule,
refresh=True),实际抓取/轮询由既有 ReportsPoller 完成——本模块不发 HTTP。
功能未配置(REPORTS_API_BASE_URL 空)时为 no-op。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from sqlite3 import Connection

from app.config import Settings
from app.services import report_jobs

log = logging.getLogger("am.reports")


def parse_sync_at(raw: str) -> tuple[int, int]:
    hour, minute = raw.split(":")
    return int(hour), int(minute)


def run_daily_sync(settings: Settings, db_factory: Callable[[], Connection]) -> int:
    """为所有候选标的创建增量任务;返回创建条数。"""
    if not settings.reports_enabled:
        log.info("[reports-sync] 未配置 REPORTS_API_BASE_URL,跳过")
        return 0
    conn = db_factory()
    try:
        jobs = report_jobs.create_sync_jobs(conn, settings=settings)
    finally:
        conn.close()
    if jobs:
        log.info("[reports-sync] 创建 %d 个增量任务:%s", len(jobs), ",".join(j["code"] for j in jobs))
    else:
        log.info("[reports-sync] 无候选(全部未回填或已有活动任务)")
    return len(jobs)
