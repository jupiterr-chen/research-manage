"""run 的领域服务(DESIGN §4.4 / R-SVC-04~10)——本系统核心业务逻辑。

所有写操作单事务完成,任何失败零副作用(AM-01)。
docker/HTTP 一概不碰;Worker 专用接口(pick_next_queued 等)只被工作线程调用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app import audit, db, models
from app.config import Settings
from app.services.errors import (
    AlreadyDoneError,
    BusyError,
    ConflictError,
    NotFound,
    ValidationError,
)

__all__ = [
    "BusyError",
    "AlreadyDoneError",
    "ConflictError",
    "NotFound",
    "ValidationError",
    "create_run",
    "enqueue_scheduled",
    "request_cancel",
    "resume",
    "get",
    "list_runs",
    "current_and_queue",
    "artifacts",
    "pick_next_queued",
    "mark_running",
    "update_progress",
    "finalize",
    "orphans_running",
]

_RUN_COLS = (
    "r.id, r.instrument_id, r.profile_id, r.analysts_csv, r.analysis_date, r.status,"
    " r.trigger, r.resumed_from, r.container_id, r.exit_code, r.current_agent,"
    " r.agents_done, r.agents_total, r.tokens_in, r.tokens_out, r.report_ready,"
    " r.status_stale, r.cancel_requested_at, r.error, r.started_at, r.finished_at,"
    " r.created_at, i.market, i.code, i.name, i.enabled AS instrument_enabled"
)


def _run_sql(where: str = "", extra: str = "") -> str:
    return f"SELECT {_RUN_COLS} FROM run r JOIN instrument i ON i.id = r.instrument_id {where} {extra}"


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("report_ready", "status_stale", "instrument_enabled"):
        if key in d and d[key] is not None:
            d[key] = bool(d[key])
    return d


def _now() -> str:
    return models.now_sh().isoformat(timespec="seconds")


def _resolve_instrument(conn: sqlite3.Connection, code: str, market: str | None) -> dict:
    """按 code(可选 market)查标的;未找到/歧义/停用 → 相应异常。"""
    candidates = [code.strip()]
    if market is not None:
        candidates = [models.normalize_code(market, code.strip())]
    else:
        # 无 market 时按各市场规则归一化,兼容 810.hk 之类的输入
        for m in models.MARKETS:
            try:
                candidates.append(models.normalize_code(m, code.strip()))
            except ValueError:
                continue
    rows = []
    for cand in dict.fromkeys(candidates):
        sql = "SELECT id, market, code, name, enabled FROM instrument WHERE code=?"
        params: list[Any] = [cand]
        if market is not None:
            sql += " AND market=?"
            params.append(market)
        rows.extend(dict(r) for r in conn.execute(sql, params).fetchall())
    if not rows:
        raise NotFound(f"标的 {code!r} 不存在,请先在「标的与调度」页添加")
    if len(rows) > 1:
        listing = ", ".join(f"{r['market']}:{r['code']}" for r in rows)
        raise ConflictError(f"代码 {code!r} 在多个市场存在({listing}),请指定 market")
    inst = rows[0]
    if market is not None:
        models.validate_code(market, inst["code"])
    if not inst["enabled"]:
        raise ConflictError(f"标的 {inst['code']} 已停用,不能发起研究")
    return inst


def _resolve_analysts(
    conn: sqlite3.Connection,
    inst: dict,
    analysts: tuple[str, ...] | list[str] | None,
    profile_id: int | None,
) -> tuple[tuple[str, ...], int | None]:
    """analysts 与 profile_id 二选一;都空时取标的第一条 enabled 调度的档案,再取默认档案。"""
    if analysts is not None and profile_id is not None:
        raise ValidationError("analysts 与 profile_id 只能二选一")
    if analysts is not None:
        items = tuple(analysts)
        unknown = [a for a in items if a not in models.ANALYSTS]
        if unknown or not items:
            raise ValidationError(
                f"非法分析师集合 {','.join(unknown) or '(空)'};合法值 {','.join(models.ANALYSTS)}"
            )
        return models.parse_analysts(",".join(items)), None
    if profile_id is not None:
        row = conn.execute("SELECT analysts_csv FROM profile WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise NotFound(f"档案 id={profile_id} 不存在")
        return models.parse_analysts(row["analysts_csv"]), profile_id
    # 都空:标的第一条 enabled 调度 → 默认档案
    row = conn.execute(
        "SELECT p.analysts_csv, p.id FROM schedule s JOIN profile p ON p.id = s.profile_id"
        " WHERE s.instrument_id=? AND s.enabled=1 AND"
        "       EXISTS(SELECT 1 FROM instrument i WHERE i.id=s.instrument_id AND i.enabled=1)"
        " ORDER BY s.id LIMIT 1",
        (inst["id"],),
    ).fetchone()
    if row:
        return models.parse_analysts(row["analysts_csv"]), row["id"]
    row = conn.execute(
        "SELECT analysts_csv, id FROM profile WHERE is_default=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        return models.parse_analysts(row["analysts_csv"]), row["id"]
    raise ValidationError("未指定分析师,且找不到可用的调度档案或默认档案")


def _busy_state(conn: sqlite3.Connection) -> tuple[dict | None, list[dict], int]:
    running = conn.execute(
        _run_sql("WHERE r.status='running'", "ORDER BY r.started_at DESC LIMIT 1")
    ).fetchone()
    queued = [
        _row_to_dict(r)
        for r in conn.execute(_run_sql("WHERE r.status='queued'", "ORDER BY r.created_at, r.id")).fetchall()
    ]
    return (_row_to_dict(running) if running else None), queued, len(queued)


def _insert_queued(
    conn: sqlite3.Connection,
    *,
    inst: dict,
    analysts: tuple[str, ...],
    analysis_date: str,
    trigger: str,
    profile_id: int | None,
    resumed_from: str | None,
    audit_detail: dict,
) -> dict:
    """生成 run id(同秒冲突 -2、-3 …)并 INSERT + 审计;须在事务内调用。"""
    now = models.now_sh()
    base_id = models.make_run_id(inst["code"], now)
    for attempt in range(0, 20):
        run_id = base_id if attempt == 0 else f"{base_id}-{attempt + 1}"
        try:
            conn.execute(
                "INSERT INTO run(id, instrument_id, profile_id, analysts_csv, analysis_date,"
                " status, trigger, resumed_from, agents_total, created_at)"
                " VALUES (?,?,?,?,?,'queued',?,?,?,?)",
                (
                    run_id,
                    inst["id"],
                    profile_id,
                    ",".join(analysts),
                    analysis_date,
                    trigger,
                    resumed_from,
                    models.agents_total(analysts),
                    now.isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.IntegrityError as e:
            msg = str(e)
            # sqlite 报错不含索引名,按约束列判别
            if "run.id" in msg:
                continue  # 同秒同 code 冲突,加后缀重试
            if "instrument_id" in msg and "analysis_date" in msg:
                raise ConflictError(f"标的 {inst['code']} 在 {analysis_date} 已有排队/运行中的任务") from e
            raise
        audit.audit(conn, trigger, "create_run", "run", run_id, audit_detail)
        row = conn.execute(_run_sql("WHERE r.id=?"), (run_id,)).fetchone()
        return _row_to_dict(row)
    raise ConflictError("run id 生成失败:同秒冲突超出重试上限")  # pragma: no cover


def create_run(
    conn: sqlite3.Connection,
    *,
    code: str,
    market: str | None = None,
    date: str | None = None,
    analysts: tuple[str, ...] | list[str] | None = None,
    profile_id: int | None = None,
    trigger: str,
    actor: str,
    force: bool = False,
) -> dict:
    """手动/API 发起(R-SVC-04 六步,单事务,失败零副作用)。"""
    if trigger not in ("web", "api"):
        raise ValidationError("create_run 仅接受 web/api 触发;调度请用 enqueue_scheduled")
    if actor not in models.TRIGGERS:
        raise ValidationError(f"非法 actor {actor!r}")
    try:
        analysis_date = models.validate_date(date) if date else models.today_sh().isoformat()
    except ValueError as e:
        raise ValidationError(str(e)) from e
    with db.tx(conn):
        # ① 标的存在且 enabled
        inst = _resolve_instrument(conn, code, market)
        # ② analysts 校验
        resolved, resolved_profile_id = _resolve_analysts(conn, inst, analysts, profile_id)
        # ③ 忙判定(仅手动触发受约束 4)
        current, queued, queued_n = _busy_state(conn)
        if current is not None or queued_n > 0:
            busy_current = current or queued[0]
            behind = queued_n if current is not None else queued_n - 1
            raise BusyError(busy_current, behind)
        # ④ already_done
        done = conn.execute(
            "SELECT id FROM run WHERE instrument_id=? AND analysis_date=? AND status='succeeded'"
            " ORDER BY finished_at DESC LIMIT 1",
            (inst["id"], analysis_date),
        ).fetchone()
        if done and not force:
            raise AlreadyDoneError(done["id"])
        # ⑤ 活动去重由 uq_run_active 兜底(③已拦截常态路径)
        # ⑥ INSERT + 审计
        return _insert_queued(
            conn,
            inst=inst,
            analysts=resolved,
            analysis_date=analysis_date,
            trigger=trigger,
            profile_id=resolved_profile_id,
            resumed_from=None,
            audit_detail={
                "code": inst["code"],
                "date": analysis_date,
                "analysts": list(resolved),
                "force": force,
            },
        )


def enqueue_scheduled(conn: sqlite3.Connection, *, schedule_id: int, date: str) -> dict | None:
    """调度入队(R-SVC-05):不受忙判定;活动去重保留分析师集合更大者。

    返回新建 run 的 dict;去重(未入队)返回 None。
    """
    try:
        analysis_date = models.validate_date(date)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    with db.tx(conn):
        sched = conn.execute(
            "SELECT s.id, s.instrument_id, s.profile_id, s.enabled AS sched_enabled,"
            "       i.code, i.enabled AS inst_enabled"
            " FROM schedule s JOIN instrument i ON i.id = s.instrument_id WHERE s.id=?",
            (schedule_id,),
        ).fetchone()
        if not sched or not sched["sched_enabled"] or not sched["inst_enabled"]:
            return None  # 调度/标的停用或不存 → 不触发(R-SCH-03)
        prof = conn.execute("SELECT analysts_csv FROM profile WHERE id=?", (sched["profile_id"],)).fetchone()
        if not prof:  # pragma: no cover - FK 保证存在
            raise NotFound(f"调度 {schedule_id} 引用的档案不存在")
        analysts = models.parse_analysts(prof["analysts_csv"])
        inst = {"id": sched["instrument_id"], "code": sched["code"]}
        active = conn.execute(
            "SELECT id, analysts_csv, status FROM run"
            " WHERE instrument_id=? AND analysis_date=? AND status IN ('queued','running')",
            (sched["instrument_id"], analysis_date),
        ).fetchone()
        if active:
            keep_new = len(analysts) > len(active["analysts_csv"].split(",")) and active["status"] == "queued"
            audit.audit(
                conn,
                "schedule",
                "deduped",
                "run",
                active["id"],
                {
                    "schedule_id": schedule_id,
                    "date": analysis_date,
                    "existing_analysts": active["analysts_csv"],
                    "new_analysts": list(analysts),
                    "replaced": keep_new,
                },
            )
            if not keep_new:
                return None
            # 已排队任务被更大的集合(全量)覆盖:删除旧的排队行(从未执行,无历史价值)
            conn.execute("DELETE FROM run WHERE id=? AND status='queued'", (active["id"],))
        return _insert_queued(
            conn,
            inst=inst,
            analysts=analysts,
            analysis_date=analysis_date,
            trigger="schedule",
            profile_id=sched["profile_id"],
            resumed_from=None,
            audit_detail={
                "code": inst["code"],
                "date": analysis_date,
                "analysts": list(analysts),
                "schedule_id": schedule_id,
            },
        )


def request_cancel(conn: sqlite3.Connection, run_id: str, actor: str) -> dict:
    """R-SVC-06:queued → 直接 cancelled;running → 置 cancel_requested_at(docker 由 Worker 执行)。"""
    with db.tx(conn):
        row = conn.execute(_run_sql("WHERE r.id=?"), (run_id,)).fetchone()
        if not row:
            raise NotFound(f"run {run_id} 不存在")
        run = _row_to_dict(row)
        if run["status"] == "queued":
            conn.execute(
                "UPDATE run SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                (_now(), run_id),
            )
            audit.audit(conn, actor, "cancel", "run", run_id, {"was": "queued"})
        elif run["status"] == "running":
            if not run["cancel_requested_at"]:
                conn.execute(
                    "UPDATE run SET cancel_requested_at=? WHERE id=? AND status='running'",
                    (_now(), run_id),
                )
                audit.audit(conn, actor, "cancel_request", "run", run_id, None)
        else:
            raise ConflictError(f"run {run_id} 状态为 {run['status']},不能取消")
        row = conn.execute(_run_sql("WHERE r.id=?"), (run_id,)).fetchone()
    return _row_to_dict(row)


def resume(conn: sqlite3.Connection, run_id: str, actor: str) -> dict:
    """R-SVC-07:仅 cancelled/failed 可续;新 run 复制 analysts_csv(断点签名要求)。"""
    with db.tx(conn):
        row = conn.execute(_run_sql("WHERE r.id=?"), (run_id,)).fetchone()
        if not row:
            raise NotFound(f"run {run_id} 不存在")
        run = _row_to_dict(row)
        if run["status"] not in ("cancelled", "failed"):
            raise ConflictError(f"run {run_id} 状态为 {run['status']},仅 cancelled/failed 可续跑")
        current, queued, queued_n = _busy_state(conn)
        if current is not None or queued_n > 0:
            raise BusyError(current or queued[0], queued_n if current is not None else queued_n - 1)
        done = conn.execute(
            "SELECT id FROM run WHERE instrument_id=? AND analysis_date=? AND status='succeeded'"
            " ORDER BY finished_at DESC LIMIT 1",
            (run["instrument_id"], run["analysis_date"]),
        ).fetchone()
        if done:
            raise AlreadyDoneError(done["id"])
        inst = {"id": run["instrument_id"], "code": run["code"]}
        if not run["instrument_enabled"]:
            raise ConflictError(f"标的 {run['code']} 已停用,不能续跑")
        analysts = tuple(run["analysts_csv"].split(","))  # 逐字复制,不重排不改写
        return _insert_queued(
            conn,
            inst=inst,
            analysts=analysts,
            analysis_date=run["analysis_date"],
            trigger=actor,
            profile_id=None,
            resumed_from=run_id,
            audit_detail={
                "code": run["code"],
                "date": run["analysis_date"],
                "analysts": list(analysts),
                "resumed_from": run_id,
            },
        )


def get(conn: sqlite3.Connection, run_id: str) -> dict:
    row = conn.execute(_run_sql("WHERE r.id=?"), (run_id,)).fetchone()
    if not row:
        raise NotFound(f"run {run_id} 不存在")
    return _row_to_dict(row)


def list_runs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    code: str | None = None,
    limit: int = 50,
) -> list[dict]:
    where, params = [], []
    if status:
        if status not in models.RUN_STATUS:
            raise ValidationError(f"非法状态 {status!r};合法值 {','.join(models.RUN_STATUS)}")
        where.append("r.status=?")
        params.append(status)
    if code:
        where.append("i.code=?")
        params.append(code.strip())
    sql = _run_sql(
        "WHERE " + " AND ".join(where) if where else "",
        "ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
    )
    params.append(max(1, min(limit, 500)))
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def current_and_queue(conn: sqlite3.Connection) -> tuple[dict | None, list[dict]]:
    current, queued, _ = _busy_state(conn)
    return current, queued


def artifacts(conn: sqlite3.Connection, settings: Settings, run_id: str) -> list[str]:
    """R-SVC-09:列出实际存在的报告文件,拼 SMB 路径返回;不读内容。"""
    run = get(conn, run_id)
    reports_dir = Path(settings.ta_data_dir) / "logs" / run["code"] / run["analysis_date"] / "reports"
    names = sorted(p.name for p in reports_dir.glob("*.md")) if reports_dir.is_dir() else []
    rel = [f"logs/{run['code']}/{run['analysis_date']}/reports/{n}" for n in names]
    prefix = settings.smb_prefix.strip()
    if not prefix:
        return rel
    sep = "\\" if "\\" in prefix else "/"
    return [prefix.rstrip("\\/") + sep + sep.join(x.split("/")) for x in rel]


# ---------- 以下仅 Worker 调用 ----------


def pick_next_queued(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(_run_sql("WHERE r.status='queued'", "ORDER BY r.created_at, r.id LIMIT 1")).fetchone()
    return _row_to_dict(row) if row else None


def mark_running(conn: sqlite3.Connection, run_id: str, container_id: str) -> None:
    with db.tx(conn):
        cur = conn.execute(
            "UPDATE run SET status='running', container_id=?, started_at=?, agents_done=0,"
            " status_stale=0 WHERE id=? AND status='queued'",
            (container_id, _now(), run_id),
        )
        if cur.rowcount == 0:
            raise ConflictError(f"run {run_id} 不在 queued 状态,无法置 running")


def update_progress(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    current_agent: str | None,
    agents_done: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    stale: bool,
) -> None:
    with db.tx(conn):
        conn.execute(
            "UPDATE run SET"
            " current_agent=COALESCE(?, current_agent),"
            " agents_done=COALESCE(?, agents_done),"
            " tokens_in=COALESCE(?, tokens_in),"
            " tokens_out=COALESCE(?, tokens_out),"
            " status_stale=? WHERE id=? AND status='running'",
            (current_agent, agents_done, tokens_in, tokens_out, int(stale), run_id),
        )


_TERMINAL_STATUS = ("succeeded", "failed", "cancelled")


def finalize(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error: str | None = None,
    report_ready: bool = False,
) -> None:
    if status not in _TERMINAL_STATUS:
        raise ValidationError(f"finalize 只接受终态 {','.join(_TERMINAL_STATUS)},收到 {status!r}")
    with db.tx(conn):
        # 不加 status='running' 守卫:launch_failed / host_restarted 需终结 queued/running
        conn.execute(
            "UPDATE run SET status=?, exit_code=?, error=?, report_ready=?, finished_at=? WHERE id=?",
            (status, exit_code, error, int(report_ready), _now(), run_id),
        )


def orphans_running(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(_run_sql("WHERE r.status='running'")).fetchall()
    return [_row_to_dict(r) for r in rows]
