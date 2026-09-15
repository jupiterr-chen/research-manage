"""服务层共享异常。

定义在独立模块供四个 service 共用;`app/services/runs.py` re-export,
使 DESIGN §4.4 的 `services.runs.BusyError` 等入口保持有效。
"""

from __future__ import annotations


class BusyError(Exception):
    """手动发起时已有 running/queued 任务(SPEC 约束 4 → 409 busy)。"""

    def __init__(self, current: dict, queued: int):
        self.current = current
        self.queued = queued
        super().__init__(f"busy: current={current.get('id')} queued={queued}")


class AlreadyDoneError(Exception):
    """同 (标的, 分析日期) 已 succeeded 且未 force(→ 409 already_done)。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"already_done: {run_id}")


class ConflictError(Exception):
    """状态不允许 / 唯一性冲突(→ 409)。"""


class NotFound(Exception):
    """实体不存在(→ 404)。"""


class ValidationError(Exception):
    """参数非法(→ 400)。"""
