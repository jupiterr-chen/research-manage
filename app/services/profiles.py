"""档案(分析师组合模板)CRUD(R-SVC-02)。"""

from __future__ import annotations

import sqlite3

from app import audit, db, models
from app.services.errors import ConflictError, NotFound, ValidationError

_COLS = "id, name, analysts_csv, is_default"


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["is_default"] = bool(d["is_default"])
    d["analysts"] = tuple(models.parse_analysts(d["analysts_csv"]))
    return d


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    analysts_csv: str,
    is_default: bool = False,
    actor: str,
) -> dict:
    name = name.strip()
    if not name:
        raise ValidationError("档案名不能为空")
    try:
        analysts = models.parse_analysts(analysts_csv)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    with db.tx(conn):
        dup = conn.execute("SELECT id FROM profile WHERE name=?", (name,)).fetchone()
        if dup:
            raise ConflictError(f"档案 {name!r} 已存在(id={dup['id']})")
        if is_default:
            conn.execute("UPDATE profile SET is_default=0")
        cur = conn.execute(
            "INSERT INTO profile(name, analysts_csv, is_default) VALUES (?,?,?)",
            (name, ",".join(analysts), int(is_default)),
        )
        profile_id = cur.lastrowid
        audit.audit(
            conn,
            actor,
            "create",
            "profile",
            str(profile_id),
            {"name": name, "analysts_csv": ",".join(analysts), "is_default": is_default},
        )
        row = conn.execute(f"SELECT {_COLS} FROM profile WHERE id=?", (profile_id,)).fetchone()
    return _row_to_dict(row)


def update(
    conn: sqlite3.Connection,
    profile_id: int,
    *,
    name: str | None = None,
    analysts_csv: str | None = None,
    is_default: bool | None = None,
    actor: str,
) -> dict:
    with db.tx(conn):
        row = conn.execute(f"SELECT {_COLS} FROM profile WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise NotFound(f"档案 id={profile_id} 不存在")
        sets, params = [], []
        detail: dict = {}
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("档案名不能为空")
            dup = conn.execute("SELECT id FROM profile WHERE name=? AND id<>?", (name, profile_id)).fetchone()
            if dup:
                raise ConflictError(f"档案 {name!r} 已存在(id={dup['id']})")
            sets.append("name=?")
            params.append(name)
            detail["name"] = name
        if analysts_csv is not None:
            try:
                analysts = models.parse_analysts(analysts_csv)
            except ValueError as e:
                raise ValidationError(str(e)) from e
            sets.append("analysts_csv=?")
            params.append(",".join(analysts))
            detail["analysts_csv"] = ",".join(analysts)
        if is_default is not None:
            if is_default:
                conn.execute("UPDATE profile SET is_default=0")
            sets.append("is_default=?")
            params.append(int(is_default))
            detail["is_default"] = is_default
        if sets:
            params.append(profile_id)
            conn.execute(f"UPDATE profile SET {','.join(sets)} WHERE id=?", params)
            audit.audit(conn, actor, "update", "profile", str(profile_id), detail)
        row = conn.execute(f"SELECT {_COLS} FROM profile WHERE id=?", (profile_id,)).fetchone()
    return _row_to_dict(row)


def delete(conn: sqlite3.Connection, profile_id: int, *, actor: str) -> None:
    import sqlite3 as _sq

    with db.tx(conn):
        row = conn.execute("SELECT id, name FROM profile WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise NotFound(f"档案 id={profile_id} 不存在")
        used_by_schedule = conn.execute(
            "SELECT COUNT(*) AS n FROM schedule WHERE profile_id=?", (profile_id,)
        ).fetchone()["n"]
        if used_by_schedule:
            raise ConflictError(f"档案 {row['name']!r} 被 {used_by_schedule} 条调度引用,请先移除")
        try:
            conn.execute("DELETE FROM profile WHERE id=?", (profile_id,))
        except _sq.IntegrityError as e:
            # run.profile_id 历史引用
            raise ConflictError(f"档案 {row['name']!r} 被历史 run 引用,不能删除") from e
        audit.audit(conn, actor, "delete", "profile", str(profile_id), {"name": row["name"]})


def get(conn: sqlite3.Connection, profile_id: int) -> dict:
    row = conn.execute(f"SELECT {_COLS} FROM profile WHERE id=?", (profile_id,)).fetchone()
    if not row:
        raise NotFound(f"档案 id={profile_id} 不存在")
    return _row_to_dict(row)


def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    return [
        _row_to_dict(r)
        for r in conn.execute(f"SELECT {_COLS} FROM profile ORDER BY is_default DESC, id").fetchall()
    ]


def default_profile(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(f"SELECT {_COLS} FROM profile WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
    return _row_to_dict(row) if row else None
