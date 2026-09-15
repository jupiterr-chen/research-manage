"""调度 CRUD 与自然语言复述(R-SVC-03)。

注意(S4 将替换):写操作后需调用 `app.state.scheduler.rebuild_jobs()`;
P0 阶段调度器为 stub,未接入,故此处不调用。
"""

from __future__ import annotations

import re
import sqlite3

from app import audit, db, models
from app.services.errors import NotFound, ValidationError

KINDS = ("daily_trading", "weekly")
KIND_LABEL = {"daily_trading": "每交易日", "weekly": "每周"}
WEEKDAY_LABEL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
_AT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_COLS = "id, instrument_id, profile_id, kind, at_time, weekday, enabled"


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


def _validate(conn: sqlite3.Connection, *, kind: str, at_time: str, weekday: int | None) -> None:
    if kind not in KINDS:
        raise ValidationError(f"调度类型 {kind!r} 非法;合法值 {','.join(KINDS)}")
    if not _AT_TIME_RE.match(at_time):
        raise ValidationError(f"触发时间 {at_time!r} 非法,应为 HH:MM(00:00~23:59)")
    if kind == "weekly":
        if weekday is None or not 1 <= weekday <= 7:
            raise ValidationError("周频调度必须指定 weekday(1=周一 … 7=周日)")
    elif weekday is not None:
        raise ValidationError("日频调度不应指定 weekday")


def create(
    conn: sqlite3.Connection,
    *,
    instrument_id: int,
    profile_id: int,
    kind: str,
    at_time: str = "08:30",
    weekday: int | None = None,
    enabled: bool = True,
    actor: str,
) -> dict:
    _validate(conn, kind=kind, at_time=at_time, weekday=weekday)
    with db.tx(conn):
        if not conn.execute("SELECT 1 FROM instrument WHERE id=?", (instrument_id,)).fetchone():
            raise NotFound(f"标的 id={instrument_id} 不存在")
        if not conn.execute("SELECT 1 FROM profile WHERE id=?", (profile_id,)).fetchone():
            raise NotFound(f"档案 id={profile_id} 不存在")
        cur = conn.execute(
            "INSERT INTO schedule(instrument_id, profile_id, kind, at_time, weekday, enabled)"
            " VALUES (?,?,?,?,?,?)",
            (instrument_id, profile_id, kind, at_time, weekday, int(enabled)),
        )
        schedule_id = cur.lastrowid
        audit.audit(
            conn,
            actor,
            "create",
            "schedule",
            str(schedule_id),
            {
                "instrument_id": instrument_id,
                "profile_id": profile_id,
                "kind": kind,
                "at_time": at_time,
                "weekday": weekday,
            },
        )
        row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
    return _row_to_dict(row)


def update(
    conn: sqlite3.Connection,
    schedule_id: int,
    *,
    profile_id: int | None = None,
    kind: str | None = None,
    at_time: str | None = None,
    weekday: int | None = None,
    enabled: bool | None = None,
    actor: str,
) -> dict:
    with db.tx(conn):
        row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise NotFound(f"调度 id={schedule_id} 不存在")
        new_kind = kind if kind is not None else row["kind"]
        new_at = at_time if at_time is not None else row["at_time"]
        # weekly→daily 时旧 weekday 置空;daily→weekly 未给 weekday 时报错
        if kind == "daily_trading":
            new_weekday = None
        elif new_kind == "weekly" and weekday is None and row["weekday"] is None:
            raise ValidationError("周频调度必须指定 weekday(1=周一 … 7=周日)")
        elif kind is None and weekday is None:
            new_weekday = row["weekday"]
        else:
            new_weekday = weekday
        _validate(conn, kind=new_kind, at_time=new_at, weekday=new_weekday)
        sets = ["kind=?", "at_time=?", "weekday=?"]
        params: list = [new_kind, new_at, new_weekday]
        detail: dict = {"kind": new_kind, "at_time": new_at, "weekday": new_weekday}
        if profile_id is not None:
            if not conn.execute("SELECT 1 FROM profile WHERE id=?", (profile_id,)).fetchone():
                raise NotFound(f"档案 id={profile_id} 不存在")
            sets.append("profile_id=?")
            params.append(profile_id)
            detail["profile_id"] = profile_id
        if enabled is not None:
            sets.append("enabled=?")
            params.append(int(enabled))
            detail["enabled"] = enabled
        params.append(schedule_id)
        conn.execute(f"UPDATE schedule SET {','.join(sets)} WHERE id=?", params)
        audit.audit(conn, actor, "update", "schedule", str(schedule_id), detail)
        row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
    return _row_to_dict(row)


def delete(conn: sqlite3.Connection, schedule_id: int, *, actor: str) -> None:
    with db.tx(conn):
        if not conn.execute("SELECT 1 FROM schedule WHERE id=?", (schedule_id,)).fetchone():
            raise NotFound(f"调度 id={schedule_id} 不存在")
        conn.execute("DELETE FROM schedule WHERE id=?", (schedule_id,))
        audit.audit(conn, actor, "delete", "schedule", str(schedule_id), None)


def toggle(conn: sqlite3.Connection, schedule_id: int, *, actor: str) -> dict:
    with db.tx(conn):
        row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise NotFound(f"调度 id={schedule_id} 不存在")
        new_enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE schedule SET enabled=? WHERE id=?", (new_enabled, schedule_id))
        audit.audit(conn, actor, "toggle", "schedule", str(schedule_id), {"enabled": bool(new_enabled)})
        row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
    return _row_to_dict(row)


def get(conn: sqlite3.Connection, schedule_id: int) -> dict:
    row = conn.execute(f"SELECT {_COLS} FROM schedule WHERE id=?", (schedule_id,)).fetchone()
    if not row:
        raise NotFound(f"调度 id={schedule_id} 不存在")
    return _row_to_dict(row)


def list_schedules(conn: sqlite3.Connection, *, instrument_id: int | None = None) -> list[dict]:
    sql = f"SELECT {_COLS} FROM schedule"
    params: list = []
    if instrument_id is not None:
        sql += " WHERE instrument_id=?"
        params.append(instrument_id)
    sql += " ORDER BY instrument_id, id"
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def describe(conn: sqlite3.Connection, schedule: dict) -> str:
    """自然语言复述,如「小米集团:每交易日 08:30 · 技术+舆情+新闻」。"""
    inst = conn.execute(
        "SELECT code, name FROM instrument WHERE id=?", (schedule["instrument_id"],)
    ).fetchone()
    prof = conn.execute(
        "SELECT name, analysts_csv FROM profile WHERE id=?", (schedule["profile_id"],)
    ).fetchone()
    title = (inst["name"] or inst["code"]) if inst else f"标的#{schedule['instrument_id']}"
    when = KIND_LABEL.get(schedule["kind"], schedule["kind"])
    if schedule["kind"] == "weekly" and schedule.get("weekday") in WEEKDAY_LABEL:
        when = f"每周{WEEKDAY_LABEL[schedule['weekday']]}"
    analysts = "技术"  # 兜底:档案缺失时仍能给出可读文案
    if prof:
        try:
            items = models.parse_analysts(prof["analysts_csv"])
            analysts = "+".join(models.ANALYST_LABEL[a] for a in items)
        except ValueError:
            analysts = prof["analysts_csv"]
    suffix = "" if schedule.get("enabled", True) else "(已停用)"
    return f"{title}:{when} {schedule['at_time']} · {analysts}{suffix}"
