"""保留策略(DESIGN §1 / R-EXE-11):清理过期 run 记录与 runs/<id>/ 目录。

每日触发由调度器接线(S4);本模块只提供幂等的 purge()。
queued/running 永不清理;只按 finished_at 早于 retention_days 判定。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

from app import audit, db, models
from app.config import Settings


def purge(conn: sqlite3.Connection, settings: Settings) -> int:
    """删除 finished_at 早于 AM_RETENTION_DAYS 的 run 与其产物目录;返回清理条数。"""
    cutoff = (models.now_sh() - timedelta(days=settings.retention_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id FROM run WHERE status IN ('succeeded','failed','cancelled')"
        " AND finished_at IS NOT NULL AND finished_at < ?",
        (cutoff,),
    ).fetchall()
    purged = 0
    for row in rows:
        run_id = row["id"]
        with db.tx(conn):
            conn.execute("DELETE FROM run WHERE id=?", (run_id,))
            audit.audit(
                conn, "schedule", "purged", "run", run_id, {"retention_days": settings.retention_days}
            )
        run_dir = Path(settings.data_dir) / "runs" / run_id
        if run_dir.is_dir():
            shutil.rmtree(run_dir, ignore_errors=True)
        purged += 1
    return purged
