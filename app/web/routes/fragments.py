"""htmx 片段路由(DESIGN §4.10 / SPEC §7 / R-WEB-02~06)。

约定:每个端点返回一个可 `hx-swap="outerHTML"` 的 `<div id=…>` 块;
错误渲染在片段内 `.flash-error`(HTTP 200 内联,不跳转);
破坏性按钮由模板侧 `hx-confirm` 二次确认。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.templating import Jinja2Templates

from app import models
from app.services import instruments as inst_srv
from app.services import profiles as prof_srv
from app.services import runs as runs_srv
from app.services import schedules as sch_srv
from app.services.errors import (
    AlreadyDoneError,
    BusyError,
    ConflictError,
    NotFound,
    ValidationError,
)
from app.web import auth
from app.web.deps import get_current_actor, get_db

router = APIRouter(prefix="/fragments", tags=["fragments"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

RECENT_LIMIT = 20


def _actor(request: Request) -> str:
    """cookie=web、Bearer=api;本地模式(local)审计归为 web(控制台用户)。"""
    actor = getattr(request.state, "actor", "web")
    return actor if actor in ("web", "api") else "web"


def _elapsed_text(run: dict) -> str:
    start = run.get("started_at")
    if not start:
        return "—"
    try:
        began = datetime.fromisoformat(start)
    except ValueError:
        return "—"
    end = run.get("finished_at")
    try:
        ended = datetime.fromisoformat(end) if end else models.now_sh()
    except ValueError:
        return "—"
    secs = int((ended - began).total_seconds())
    if secs < 0:
        return "—"
    return f"{secs // 60} 分 {secs % 60} 秒"


def _run_view(run: dict) -> dict:
    d = dict(run)
    d["date"] = run["analysis_date"]  # 模板统一用 date
    d["analysts_list"] = run["analysts_csv"].split(",")
    d["elapsed"] = _elapsed_text(d)
    d["timeline"] = models.agent_sequence(d["analysts_list"])
    return d


def _overview_ctx(db, request) -> dict:
    current, queued = runs_srv.current_and_queue(db)
    recent = runs_srv.list_runs(db, limit=RECENT_LIMIT)
    today = models.today_sh()
    fires = request.app.state.scheduler.next_fires(today)
    instruments = inst_srv.list_instruments(db)
    return {
        "current": _run_view(current) if current else None,
        "queued": [_run_view(q) for q in queued],
        "recent": [_run_view(r) for r in recent],
        "today": today.isoformat(),
        "next_fires": fires,
        "instruments": instruments,
        "pre_market_hint": "盘前",
    }


# ---------- 总览 ----------


@router.get("/overview", dependencies=[Depends(auth.require_auth)])
async def overview(request: Request, db=Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="fragments/overview.html", context=_overview_ctx(db, request)
    )


@router.post("/run", dependencies=[Depends(auth.require_auth)])
async def create_run(
    request: Request,
    db=Depends(get_db),
    actor: str = Depends(get_current_actor),
    code: str = Form(default=""),
    date: str = Form(default=""),
    mode: str = Form(default="profile"),
    profile_id: int | None = Form(default=None),
    analysts: list[str] = Form(default=[]),
    force: bool = Form(default=False),
):
    """手动发起(R-WEB-03):成功 → OOB 刷新 overview;忙/参数错 → 片段内联提示。"""
    try:
        run = runs_srv.create_run(
            db,
            code=code,
            date=date or None,
            analysts=tuple(analysts) if mode == "analysts" else None,
            profile_id=profile_id if mode == "profile" else None,
            trigger="web",
            actor=_actor(request),
            force=force,
        )
    except BusyError as exc:
        return templates.TemplateResponse(
            request=request,
            name="fragments/run_result.html",
            context={"ok": False, "error": "busy", "current": _run_view(exc.current), "queued": exc.queued},
        )
    except AlreadyDoneError:
        return templates.TemplateResponse(
            request=request,
            name="fragments/run_result.html",
            context={
                "ok": False,
                "error": "already_done",
                "message": "该标的在此日期已成功;完整重跑需勾选「强制重跑」",
            },
        )
    except (ValidationError, ValueError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="fragments/run_result.html",
            context={"ok": False, "error": "invalid", "message": str(exc)},
        )
    except NotFound as exc:
        return templates.TemplateResponse(
            request=request,
            name="fragments/run_result.html",
            context={"ok": False, "error": "invalid", "message": str(exc)},
        )
    # 成功:结果区提示 + OOB 整体刷新总览(overview ctx 平铺供 include 使用)
    ctx = {"ok": True, "run": _run_view(run), "oob": True}
    ctx.update(_overview_ctx(db, request))
    return templates.TemplateResponse(request=request, name="fragments/run_result.html", context=ctx)


# ---------- 标的 ----------


def _instrument_block_ctx(db, request, message=None, error=None) -> dict:
    instruments = inst_srv.list_instruments(db)
    schedules_by_inst: dict[int, list[dict]] = {}
    describe_by_sched: dict[int, str] = {}
    monday_hint: dict[int, bool] = {}
    for s in sch_srv.list_schedules(db):
        schedules_by_inst.setdefault(s["instrument_id"], []).append(s)
        describe_by_sched[s["id"]] = sch_srv.describe(db, s)
    for inst in instruments:
        rows = schedules_by_inst.get(inst["id"], [])
        kinds = {s["kind"] for s in rows if s["enabled"]}
        monday_hint[inst["id"]] = "daily_trading" in kinds and any(
            s["kind"] == "weekly" and s["weekday"] == 1 for s in rows if s["enabled"]
        )
    return {
        "instruments": instruments,
        "schedules_by_inst": schedules_by_inst,
        "describe_by_sched": describe_by_sched,
        "monday_hint": monday_hint,
        "profiles": prof_srv.list_profiles(db),
        "markets": models.MARKETS,
        "weekday_label": sch_srv.WEEKDAY_LABEL,
        "message": message,
        "error": error,
    }


def _instrument_block(request: Request, db, **kw):
    return templates.TemplateResponse(
        request=request,
        name="fragments/instruments_block.html",
        context=_instrument_block_ctx(db, request, **kw),
    )


@router.post("/instruments", dependencies=[Depends(auth.require_auth)])
async def create_instrument(
    request: Request,
    db=Depends(get_db),
    market: str = Form(default=""),
    code: str = Form(default=""),
    name: str = Form(default=""),
):
    try:
        inst_srv.create(db, market=market, code=code, name=name, actor=_actor(request))
    except (ValidationError, ValueError, ConflictError) as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db, message=f"已添加标的 {code.strip().upper()}")


@router.post("/instruments/{instrument_id}", dependencies=[Depends(auth.require_auth)])
async def update_instrument(
    request: Request,
    instrument_id: int,
    db=Depends(get_db),
    name: str | None = Form(default=None),
    enabled: bool | None = Form(default=None),
):
    try:
        inst_srv.update(
            db,
            instrument_id,
            name=name,
            enabled=enabled if enabled is not None else None,
            actor=_actor(request),
        )
    except (ValidationError, ValueError, ConflictError, NotFound) as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db)


@router.delete("/instruments/{instrument_id}", dependencies=[Depends(auth.require_auth)])
async def delete_instrument(request: Request, instrument_id: int, db=Depends(get_db)):
    try:
        inst_srv.delete(db, instrument_id, actor=_actor(request))
    except ConflictError as exc:
        return _instrument_block(request, db, error=str(exc))
    except NotFound as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db, message="标的已删除")


# ---------- 调度 ----------


@router.post("/schedules", dependencies=[Depends(auth.require_auth)])
async def create_schedule(
    request: Request,
    db=Depends(get_db),
    instrument_id: int = Form(default=0),
    profile_id: int = Form(default=0),
    kind: str = Form(default="daily_trading"),
    at_time: str = Form(default="08:30"),
    weekday: int | None = Form(default=None),
):
    try:
        sch_srv.create(
            db,
            instrument_id=instrument_id,
            profile_id=profile_id,
            kind=kind,
            at_time=at_time,
            weekday=weekday,
            actor=_actor(request),
        )
    except (ValidationError, ValueError, NotFound) as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db, message="调度已添加")


@router.post("/schedules/{schedule_id}", dependencies=[Depends(auth.require_auth)])
async def update_schedule(
    request: Request,
    schedule_id: int,
    db=Depends(get_db),
    profile_id: int | None = Form(default=None),
    kind: str | None = Form(default=None),
    at_time: str | None = Form(default=None),
    weekday: int | None = Form(default=None),
):
    try:
        sch_srv.update(
            db,
            schedule_id,
            profile_id=profile_id,
            kind=kind,
            at_time=at_time,
            weekday=weekday,
            actor=_actor(request),
        )
    except (ValidationError, ValueError, NotFound) as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db, message="调度已更新")


@router.post("/schedules/{schedule_id}/toggle", dependencies=[Depends(auth.require_auth)])
async def toggle_schedule(request: Request, schedule_id: int, db=Depends(get_db)):
    try:
        sch_srv.toggle(db, schedule_id, actor=_actor(request))
    except (ValidationError, NotFound) as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db)


@router.delete("/schedules/{schedule_id}", dependencies=[Depends(auth.require_auth)])
async def delete_schedule(request: Request, schedule_id: int, db=Depends(get_db)):
    try:
        sch_srv.delete(db, schedule_id, actor=_actor(request))
    except NotFound as exc:
        return _instrument_block(request, db, error=str(exc))
    return _instrument_block(request, db, message="调度已删除")


# ---------- 运行详情 ----------


def _run_detail_ctx(db, request, run_id: str) -> dict | None:
    try:
        run = runs_srv.get(db, run_id)
    except NotFound:
        return None
    view = _run_view(run)
    settings = request.app.state.settings
    view["container_log"] = str(Path(settings.data_dir) / "runs" / run_id / "container.log")
    view["terminal"] = run["status"] in ("succeeded", "failed", "cancelled")
    view["can_cancel"] = run["status"] in ("queued", "running")
    view["can_resume"] = run["status"] in ("cancelled", "failed")
    return {"run": view}


@router.get("/runs/{run_id}", dependencies=[Depends(auth.require_auth)])
async def run_detail_fragment(request: Request, run_id: str, db=Depends(get_db)):
    """时间线+进度;终态后停止轮询(hx-trigger 由模板按状态决定)。"""
    ctx = _run_detail_ctx(db, request, run_id)
    if ctx is None:
        return templates.TemplateResponse(
            request=request,
            name="fragments/run_flash.html",
            context={"error": f"run {run_id} 不存在"},
            status_code=200,
        )
    return templates.TemplateResponse(request=request, name="fragments/run_detail.html", context=ctx)


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(auth.require_auth)])
async def cancel_run(request: Request, run_id: str, db=Depends(get_db)):
    try:
        runs_srv.request_cancel(db, run_id, actor=_actor(request))
    except (ConflictError, NotFound) as exc:
        ctx = _run_detail_ctx(db, request, run_id)
        return templates.TemplateResponse(
            request=request, name="fragments/run_detail.html", context={**ctx, "flash_error": str(exc)}
        )
    ctx = _run_detail_ctx(db, request, run_id)
    return templates.TemplateResponse(
        request=request,
        name="fragments/run_detail.html",
        context={**ctx, "flash_ok": "已请求取消:排队任务立即取消;运行中任务将在保存断点后停止"},
    )


@router.post("/runs/{run_id}/resume", dependencies=[Depends(auth.require_auth)])
async def resume_run(request: Request, run_id: str, db=Depends(get_db)):
    try:
        new_run = runs_srv.resume(db, run_id, actor=_actor(request))
    except (BusyError, AlreadyDoneError, ConflictError, NotFound) as exc:
        ctx = _run_detail_ctx(db, request, run_id)
        return templates.TemplateResponse(
            request=request, name="fragments/run_detail.html", context={**ctx, "flash_error": str(exc)}
        )
    ctx = _run_detail_ctx(db, request, new_run["id"])
    return templates.TemplateResponse(
        request=request,
        name="fragments/run_detail.html",
        context={**ctx, "flash_ok": f"已创建续跑任务 {new_run['id']}"},
    )
