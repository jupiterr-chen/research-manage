"""标的 CRUD(R-SVC-01)。纯 DB 业务逻辑:无 docker、无 HTTP。"""

from __future__ import annotations

import sqlite3

from app import audit, db, models
from app.services.errors import ConflictError, NotFound, ValidationError

_COLS = "id, market, code, name, enabled, created_at, updated_at"


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


def create(
    conn: sqlite3.Connection,
    *,
    market: str,
    code: str,
    name: str = "",
    enabled: bool = True,
    actor: str,
) -> dict:
    if market not in models.MARKETS:
        raise ValidationError(f"未知市场 {market!r};合法值 {','.join(models.MARKETS)}")
    try:
        normalized = models.normalize_code(market, code)
        models.validate_code(market, normalized)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    now = models.now_sh().isoformat(timespec="seconds")
    with db.tx(conn):
        dup = conn.execute(
            "SELECT id FROM instrument WHERE market=? AND code=?", (market, normalized)
        ).fetchone()
        if dup:
            raise ConflictError(f"标的 {market}:{normalized} 已存在(id={dup['id']})")
        cur = conn.execute(
            "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (market, normalized, name.strip(), int(enabled), now, now),
        )
        instrument_id = cur.lastrowid
        audit.audit(
            conn,
            actor,
            "create",
            "instrument",
            str(instrument_id),
            {"market": market, "code": normalized, "name": name.strip()},
        )
        row = conn.execute(f"SELECT {_COLS} FROM instrument WHERE id=?", (instrument_id,)).fetchone()
    return _row_to_dict(row)


def update(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    actor: str,
) -> dict:
    with db.tx(conn):
        row = conn.execute(f"SELECT {_COLS} FROM instrument WHERE id=?", (instrument_id,)).fetchone()
        if not row:
            raise NotFound(f"标的 id={instrument_id} 不存在")
        sets, params = ["updated_at=?"], [models.now_sh().isoformat(timespec="seconds")]
        if name is not None:
            sets.append("name=?")
            params.append(name.strip())
        if enabled is not None:
            sets.append("enabled=?")
            params.append(int(enabled))
        params.append(instrument_id)
        conn.execute(f"UPDATE instrument SET {','.join(sets)} WHERE id=?", params)
        audit.audit(
            conn,
            actor,
            "update",
            "instrument",
            str(instrument_id),
            {"name": name, "enabled": enabled} if (name is not None or enabled is not None) else None,
        )
        row = conn.execute(f"SELECT {_COLS} FROM instrument WHERE id=?", (instrument_id,)).fetchone()
    return _row_to_dict(row)


def delete(conn: sqlite3.Connection, instrument_id: int, *, actor: str) -> None:
    with db.tx(conn):
        row = conn.execute("SELECT id FROM instrument WHERE id=?", (instrument_id,)).fetchone()
        if not row:
            raise NotFound(f"标的 id={instrument_id} 不存在")
        schedules = conn.execute(
            "SELECT id, kind, at_time FROM schedule WHERE instrument_id=?", (instrument_id,)
        ).fetchall()
        if schedules:
            listing = ", ".join(f"#{s['id']}({s['kind']} {s['at_time']})" for s in schedules)
            raise ConflictError(f"标的 id={instrument_id} 有关联调度,请先删除:{listing}")
        conn.execute("DELETE FROM instrument WHERE id=?", (instrument_id,))
        audit.audit(conn, actor, "delete", "instrument", str(instrument_id), None)


def get(conn: sqlite3.Connection, instrument_id: int) -> dict:
    row = conn.execute(f"SELECT {_COLS} FROM instrument WHERE id=?", (instrument_id,)).fetchone()
    if not row:
        raise NotFound(f"标的 id={instrument_id} 不存在")
    return _row_to_dict(row)


def find_by_code(conn: sqlite3.Connection, code: str, market: str | None = None) -> dict | None:
    sql = f"SELECT {_COLS} FROM instrument WHERE code=?"
    params: list = [code]
    if market:
        sql += " AND market=?"
        params.append(market)
    row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row) if row else None


def list_instruments(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict]:
    sql = f"SELECT {_COLS} FROM instrument"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY market, code"
    return [_row_to_dict(r) for r in conn.execute(sql).fetchall()]
