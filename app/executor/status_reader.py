"""status.json 安全读取(DESIGN §4.6 / R-EXE-05 / AM-08)。

文件不存在 / JSON 损坏(含半截)/ 必要字段缺失 → None,绝不抛;
调用方(Worker)负责保持上次值并置 status_stale。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Status:
    phase: str
    current_agent: str | None
    agents_done: int
    agents_total: int
    tokens_in: int
    tokens_out: int
    updated_at: datetime | None
    error: str | None


def _parse_updated_at(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SH_TZ)
    return dt


def read_status(path: Path) -> Status | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if not isinstance(data["phase"], str):
            return None
        agents_done = data["agents_done"]
        agents_total = data["agents_total"]
        tokens_in = data["tokens_in"]
        tokens_out = data["tokens_out"]
        if not all(isinstance(v, int) for v in (agents_done, agents_total, tokens_in, tokens_out)):
            return None
        current_agent = data.get("current_agent")
        error = data.get("error")
        if current_agent is not None and not isinstance(current_agent, str):
            return None
        if error is not None and not isinstance(error, str):
            return None
    except KeyError:
        return None
    return Status(
        phase=data["phase"],
        current_agent=current_agent,
        agents_done=agents_done,
        agents_total=agents_total,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        updated_at=_parse_updated_at(data.get("updated_at")),
        error=error,
    )
