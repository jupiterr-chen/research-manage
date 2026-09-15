"""APScheduler 封装 —— P0 骨架(stub)。

真实实现(交易日语义、rebuild_jobs、next_fires)在 feat/scheduler(S4)交付;
本 stub 只实现 DESIGN §4.9 的接口面。stub 阶段没有任何 job 被注册,
`rebuild_jobs()` 如实返回 0。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from sqlite3 import Connection

from app.config import Settings


class Scheduler:
    def __init__(self, settings: Settings, db_factory: Callable[[], Connection]):
        self.settings = settings
        self.db_factory = db_factory
        self._running = False

    def start(self) -> None:
        self._running = True

    def shutdown(self) -> None:
        self._running = False

    def rebuild_jobs(self) -> int:
        """从 schedule 表全量重建,返回 job 数。stub:未注册任何 job。"""
        return 0

    def next_fires(self, today: date) -> list[dict]:
        """今日剩余触发预告。 stub:空。"""
        return []

    @property
    def jobs_count(self) -> int:
        return 0

    @property
    def running(self) -> bool:  # pragma: no cover - healthz 备用
        return self._running
