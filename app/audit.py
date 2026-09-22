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
# URL 查询串 / 键值对形式的凭据(T-07:上游把 FRED 等请求 URL 连同 api_key= 写进日志)
_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization)"
    r"([\"']?\s*[=:]\s*)(?!bearer\b|\*\*\*)([\"']?[^&\s\"'<>]+[\"']?)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_MASK = "***"


def scrub_text(text: str) -> str:
    """字符串脱敏:sk-*、`key=value` / `key: value` 形式、`Bearer xxx`。"""
    text = _SK_PATTERN.sub("sk-***", text)
    text = _BEARER_PATTERN.sub("Bearer ***", text)  # 先于 KV,避免 "Authorization: Bearer" 被当成值
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", text)
    return text


def scrub(obj: Any) -> Any:
    """递归脱敏:键名匹配 /key|token|secret|password|authorization/i 的值 → "***";
    字符串经 scrub_text()(sk-*、URL/键值对里的 api_key=…、Bearer …)。返回脱敏后的新对象。"""
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
        return scrub_text(obj)
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
