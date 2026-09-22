"""财报原文获取任务(report_job)的纯 DB 业务逻辑(不发 HTTP;HTTP 在 app.reports.poller)。

状态机(本地):
  pending ──提交成功──▶ queued/running(跟随服务端)──▶ succeeded | partial | failed
     │                                                 (服务端终态,HTTP 200 ≠ 成功,以 status 为准)
     ├──提交被 429──▶ pending(next_attempt_at=Retry-After 后;同一 Idempotency-Key 重试)
     ├──提交 4xx/409 冲突/校验错──▶ error
     └──总等待超预算──▶ timeout(服务端任务可能仍在跑;可「刷新状态」再拉一次)
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from app import audit, db, models
from app.reports import symbols
from app.services.errors import ConflictError, NotFound, ValidationError

TERMINAL = ("succeeded", "partial", "failed", "timeout", "error")
ACTIVE = ("pending", "queued", "running")
MAX_LAST_N = 20
STATUS_LABEL = {
    "pending": "待提交",
    "queued": "排队中",
    "running": "获取中",
    "succeeded": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "timeout": "等待超时",
    "error": "提交错误",
}


def _now() -> str:
    return models.now_sh().isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("progress_json", "summary_json", "results_json", "warnings_json", "report_ids_json"):
        raw = d.pop(key)
        d[key[:-5]] = json.loads(raw) if raw else None
    d["refresh"] = bool(d["refresh"])
    d["error_retryable"] = None if d["error_retryable"] is None else bool(d["error_retryable"])
    d["status_label"] = STATUS_LABEL.get(d["status"], d["status"])
    d["is_terminal"] = d["status"] in TERMINAL
    return d


def make_id(code: str, now: datetime) -> str:
    stamp = now.astimezone(models.SH_TZ).strftime("%Y%m%d-%H%M%S")
    return f"rj-{stamp}-{re.sub(r'[^A-Za-z0-9]', '', code)}"


def create(
    conn: sqlite3.Connection,
    *,
    code: str,
    market: str | None,
    last_n: int | None,
    refresh: bool,
    trigger: str,
    actor: str,
    default_last_n: int = 4,
) -> dict:
    """新建本地任务(status=pending);Idempotency-Key 在此生成并固定。
    同标的存在活动任务时 409(避免同一动作重复提交;不同标的可并行)。"""
    try:
        market, code = symbols.resolve(code, market)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    n = default_last_n if last_n is None else int(last_n)
    if not 1 <= n <= MAX_LAST_N:
        raise ValidationError(f"last_n 必须在 1..{MAX_LAST_N}")
    if trigger not in ("web", "api"):
        raise ValidationError("trigger 非法")
    with db.tx(conn):
        active = conn.execute(
            "SELECT id FROM report_job WHERE market=? AND code=?"
            " AND status IN ('pending','queued','running')",
            (market, code),
        ).fetchone()
        if active:
            raise ConflictError(f"标的 {code} 已有获取任务 {active['id']} 在进行中")
        inst = conn.execute("SELECT id FROM instrument WHERE market=? AND code=?", (market, code)).fetchone()
        now = models.now_sh()
        job_id = make_id(code, now)
        suffix = 2
        while conn.execute("SELECT 1 FROM report_job WHERE id=?", (job_id,)).fetchone():
            job_id = f"{make_id(code, now)}-{suffix}"
            suffix += 1
        key = f"am-{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO report_job(id, instrument_id, market, code, symbol, last_n, refresh,"
            " idempotency_key, status, trigger, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                inst["id"] if inst else None,
                market,
                code,
                symbols.to_fetcher_symbol(market, code),
                n,
                int(refresh),
                key,
                "pending",
                trigger,
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        audit.audit(
            conn,
            actor,
            "create",
            "report_job",
            job_id,
            {"code": code, "market": market, "last_n": n, "refresh": bool(refresh)},
        )
    return get(conn, job_id)


def get(conn: sqlite3.Connection, job_id: str) -> dict:
    row = conn.execute("SELECT * FROM report_job WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"report_job {job_id} 不存在")
    return _row(row)


def list_jobs(
    conn: sqlite3.Connection, *, status: str | None = None, code: str | None = None, limit: int = 50
):
    sql = "SELECT * FROM report_job"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if code:
        where.append("code=?")
        args.append(code.strip().upper())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    return [_row(r) for r in conn.execute(sql, args).fetchall()]


def active_jobs(conn: sqlite3.Connection) -> list[dict]:
    """轮询线程用:所有未终态任务,按创建顺序。"""
    rows = conn.execute(
        "SELECT * FROM report_job WHERE status IN ('pending','queued','running') ORDER BY created_at, id"
    ).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- 以下仅轮询线程调用
def mark_submitted(conn, job_id: str, *, remote_job_id: str, remote_status: str) -> None:
    status = remote_status if remote_status in ("queued", "running") else "queued"
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET remote_job_id=?, status=?, submit_attempts=submit_attempts+1,"
            " next_attempt_at=NULL, started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
            (remote_job_id, status, _now(), _now(), job_id),
        )


def mark_submit_retry(conn, job_id: str, *, retry_after: float, reason: str) -> None:
    """429 等可重试:记下次尝试时间,状态保持 pending,key 不变。"""
    at = (models.now_sh() + timedelta(seconds=max(1.0, retry_after))).isoformat(timespec="seconds")
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET submit_attempts=submit_attempts+1, next_attempt_at=?, error=?,"
            " updated_at=? WHERE id=? AND status='pending'",
            (at, reason[:300], _now(), job_id),
        )


def update_progress(conn, job_id: str, doc: dict) -> None:
    """同步服务端 JobStatusOut 到本地(queued/running 阶段)。"""
    status = doc.get("status")
    local = status if status in ("queued", "running") else "running"
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET status=?, progress_json=?, updated_at=? WHERE id=? AND status IN"
            " ('pending','queued','running')",
            (local, json.dumps(doc.get("progress") or {}, ensure_ascii=False), _now(), job_id),
        )


def finalize_remote(conn, job_id: str, doc: dict, actor: str = "web") -> dict:
    """服务端终态(succeeded/partial/failed):落盘 results/summary/warnings/report_ids。
    partial 与 failed 都保留 results 中可用的 report_ids(partial 必须保留可用文件)。"""
    status = doc.get("status")
    if status not in ("succeeded", "partial", "failed"):
        raise ValueError(f"非终态 {status!r}")
    results = doc.get("results") or []
    warnings: list[str] = []
    report_ids: list[str] = []
    error_code = None
    error_retryable = None
    error_text = None
    for r in results:
        symbol = r.get("symbol", "?")
        coverage = r.get("coverage")
        notices = coverage.get("notices") if isinstance(coverage, dict) else None
        texts = list(r.get("warnings") or [])
        if isinstance(notices, list):
            texts.extend(notices)
        seen: set[str] = set()
        for w in texts:
            label = f"{symbol}: {w}"
            if label in seen:
                continue
            seen.add(label)
            warnings.append(label)
        report_ids.extend(r.get("report_ids") or [])
        err = r.get("error")
        if err and error_code is None:
            error_code = err.get("code")
            error_retryable = bool(err.get("retryable"))
            error_text = f"{r.get('symbol', '?')}: {err.get('code')} {err.get('message') or ''}".strip()
    if status == "failed" and error_text is None:
        error_text = "任务失败(服务端未给出证券级错误)"
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET status=?, progress_json=?, summary_json=?, results_json=?,"
            " warnings_json=?, report_ids_json=?, error=?, error_code=?, error_retryable=?,"
            " finished_at=?, updated_at=?"
            " WHERE id=?",
            (
                status,
                json.dumps(doc.get("progress") or {}, ensure_ascii=False),
                json.dumps(doc.get("summary") or {}, ensure_ascii=False),
                json.dumps(results, ensure_ascii=False),
                json.dumps(warnings, ensure_ascii=False),
                json.dumps(report_ids, ensure_ascii=False),
                error_text,
                error_code,
                None if error_retryable is None else int(error_retryable),
                doc.get("finished_at") or _now(),
                _now(),
                job_id,
            ),
        )
        audit.audit(
            conn,
            actor,
            "finalize",
            "report_job",
            job_id,
            {
                "status": status,
                "reports": len(report_ids),
                "warnings": len(warnings),
                "error_code": error_code,
            },
        )
    return get(conn, job_id)


def finalize_local(conn, job_id: str, *, status: str, error: str, error_code: str | None = None) -> dict:
    """本地终态:timeout(预算耗尽)或 error(提交失败/冲突/连不上)。"""
    if status not in ("timeout", "error"):
        raise ValueError(status)
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET status=?, error=?, error_code=?, finished_at=?, updated_at=? WHERE id=?",
            (status, error[:500], error_code, _now(), _now(), job_id),
        )
        audit.audit(
            conn, "web", "finalize", "report_job", job_id, {"status": status, "error_code": error_code}
        )
    return get(conn, job_id)


def reopen_for_refresh(conn, job_id: str, actor: str) -> dict:
    """timeout 的任务:服务端 job 仍存在,允许重新进入 running 由轮询线程再拉终态。"""
    job = get(conn, job_id)
    if job["status"] != "timeout" or not job["remote_job_id"]:
        raise ConflictError("只有等待超时且已有服务端任务号的任务可以刷新状态")
    with db.tx(conn):
        conn.execute(
            "UPDATE report_job SET status='running', error=NULL, error_code=NULL, finished_at=NULL,"
            " started_at=?, updated_at=? WHERE id=?",
            (_now(), _now(), job_id),
        )
        audit.audit(conn, actor, "refresh", "report_job", job_id, None)
    return get(conn, job_id)
