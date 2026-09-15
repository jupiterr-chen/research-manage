"""Worker 线程 —— P0 骨架(stub)。

真实实现(docker 启停、终态判定、看门狗、恢复)在 feat/executor(S3)交付;
本 stub 只实现 DESIGN §4.8 的接口面,供 lifespan 注入与 /healthz 读取。
不调用 docker。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from app import services
from app.config import Settings

log = logging.getLogger("am.worker")


class Worker(threading.Thread):
    def __init__(self, settings: Settings, launcher, db_factory: Callable[[], Connection]):
        super().__init__(daemon=False, name="am-worker")
        self.settings = settings
        self.launcher = launcher  # S3 起使用;stub 阶段可为 None
        self.db_factory = db_factory
        # 注意不能命名为 _stop:会遮蔽 threading.Thread._stop
        self._stop_event = threading.Event()
        self._last_tick: datetime | None = None
        self._tz = ZoneInfo(settings.tz or "Asia/Shanghai")

    def run(self) -> None:  # pragma: no cover - stub 不做实际工作
        while not self._stop_event.is_set():
            self._last_tick = datetime.now(self._tz)
            self._stop_event.wait(1.0)

    def stop(self) -> None:
        """置停止标志;不 stop 执行容器(DESIGN §2)。"""
        self._stop_event.set()

    def health(self) -> dict:
        """{alive, docker_ok, queue_depth, current_run_id, last_tick}(DESIGN §4.8)。"""
        queue_depth, current_run_id = 0, None
        try:
            conn = self.db_factory()
            try:
                current, queued = services.runs.current_and_queue(conn)
                queue_depth = len(queued)
                current_run_id = current["id"] if current else None
            finally:
                conn.close()
        except Exception:
            log.debug("health 读取队列失败", exc_info=True)  # health 探测不抛
        return {
            "alive": self.is_alive(),
            "docker_ok": None,  # stub 不触碰 docker;S3 起 Worker 自检提供
            "queue_depth": queue_depth,
            "current_run_id": current_run_id,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
        }
