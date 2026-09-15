"""审计与脱敏(DESIGN §4.3 / R-FND-06)。

`detail_json` 序列化前必经 `scrub()`;审计行不含任何凭据(AM-07)。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app import models

_SENSITIVE_KEY = re.compile(r"key|token|secret|password|authorization", re.IGNORECASE)
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}")
_MASK = "***"


def scrub(obj: Any) -> Any:
    """递归脱敏:键名匹配 /key|token|secret|password|authorization/i 的值 → "***";
    字符串中的 sk-[A-Za-z0-9]{8,} → "sk-***"。返回脱敏后的新对象。"""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and _SENSITIVE_KEY.search(str(k)):
                out[str(k)] = _MASK
            else:
                out[str(k)] = scrub(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(x) for x in obj]
    if isinstance(obj, str):
        return _SK_PATTERN.sub("sk-***", obj)
    return obj


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | None,
    detail: dict[str, Any] | None,
) -> None:
    """写一条审计记录。调用方负责处于事务内(detail 先 scrub)。"""
    if actor not in models.TRIGGERS:
        raise ValueError(f"非法 actor {actor!r}")
    detail_json = json.dumps(scrub(detail), ensure_ascii=False) if detail else None
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
        (
            models.now_sh().isoformat(timespec="seconds"),
            actor,
            action,
            entity,
            str(entity_id) if entity_id is not None else None,
            detail_json,
        ),
    )
