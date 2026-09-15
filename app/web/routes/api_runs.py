"""/api/v1/runs* 六端点(SPEC §5 / DESIGN §4.10 / R-API-01~05)。

业务全部委托 services.runs,本层不写第二套校验(仅做 400/404 分流用的
代码格式识别与 JSON 反序列化);写操作 actor=api。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import models
from app.services import runs as runs_srv
from app.services.errors import AlreadyDoneError, BusyError, ConflictError, NotFound
from app.services.errors import ValidationError as ServiceValidationError
from app.web import auth
from app.web.deps import get_db, get_settings

router = APIRouter(prefix="/api/v1", tags=["api"])

_CURRENT_FIELDS = ("id", "code", "date", "current_agent", "agents_done", "agents_total", "started_at")


def _busy_body(exc: BusyError) -> dict:
    current = {key: exc.current.get("analysis_date" if key == "date" else key) for key in _CURRENT_FIELDS}
    return {
        "error": "busy",
        "message": "已有任务在执行或排队,请稍后再试",
        "current": current,
        "queued": exc.queued,
    }


def _error_response(status: int, error: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message, **extra})


class RunCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    date: str | None = None
    analysts: list[str] | None = None
    profile_id: int | None = None
    force: bool = False


def _run_payload(run: dict) -> dict:
    return {
        "id": run["id"],
        "code": run["code"],
        "market": run.get("market"),
        "date": run["analysis_date"],
        "analysts": run["analysts_csv"].split(","),
        "profile_id": run.get("profile_id"),
        "status": run["status"],
        "trigger": run["trigger"],
        "resumed_from": run.get("resumed_from"),
        "current_agent": run.get("current_agent"),
        "agents_done": run.get("agents_done", 0),
        "agents_total": run["agents_total"],
        "tokens_in": run.get("tokens_in", 0),
        "tokens_out": run.get("tokens_out", 0),
        "report_ready": bool(run.get("report_ready", 0)),
        "status_stale": bool(run.get("status_stale", 0)),
        "cancel_requested_at": run.get("cancel_requested_at"),
        "exit_code": run.get("exit_code"),
        "error": run.get("error"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "created_at": run.get("created_at"),
    }


@router.post("/runs", dependencies=[Depends(auth.require_auth)])
async def create_run(body: RunCreateRequest, request: Request, db=Depends(get_db)) -> JSONResponse:
    """发起一次研究(SPEC §5)。忙时 409 + current/queued,零副作用。"""
    code = body.code.strip().upper()
    if models.code_market(code) is None:
        return _error_response(
            400, "invalid_request", "代码格式非法:us 裸代码(如 NVDA)、hk ####.HK、cn ######.SS/.SZ"
        )
    try:
        run = runs_srv.create_run(
            db,
            code=code,
            date=body.date,
            analysts=tuple(body.analysts) if body.analysts is not None else None,
            profile_id=body.profile_id,
            trigger="api",
            actor="api",
            force=body.force,
        )
    except BusyError as exc:
        return JSONResponse(status_code=409, content=_busy_body(exc))
    except AlreadyDoneError as exc:
        return _error_response(
            409, "already_done", "该标的在此日期已成功完成;如需完整重跑请带 force=true", run_id=exc.run_id
        )
    except ServiceValidationError as exc:
        return _error_response(400, "invalid_request", str(exc))
    except NotFound as exc:
        return _error_response(404, "not_found", str(exc))
    except ConflictError as exc:
        return _error_response(409, "conflict", str(exc))
    return JSONResponse(status_code=200, content=_run_payload(run))


@router.get("/runs", dependencies=[Depends(auth.require_auth)])
async def list_runs(
    status: str | None = None, code: str | None = None, limit: int = 50, db=Depends(get_db)
) -> JSONResponse:
    try:
        items = runs_srv.list_runs(db, status=status, code=code, limit=limit)
    except ServiceValidationError as exc:
        return _error_response(400, "invalid_request", str(exc))
    return JSONResponse(status_code=200, content={"runs": [_run_payload(r) for r in items]})


@router.get("/runs/{run_id}", dependencies=[Depends(auth.require_auth)])
async def get_run(run_id: str, db=Depends(get_db)) -> JSONResponse:
    try:
        run = runs_srv.get(db, run_id)
    except NotFound as exc:
        return _error_response(404, "not_found", str(exc))
    return JSONResponse(status_code=200, content=_run_payload(run))


@router.get("/runs/{run_id}/artifacts", dependencies=[Depends(auth.require_auth)])
async def get_artifacts(
    run_id: str, request: Request, db=Depends(get_db), settings=Depends(get_settings)
) -> JSONResponse:
    """报告文件路径列表(AM-18:只给路径,绝不返回内容)。"""
    try:
        paths = runs_srv.artifacts(db, settings, run_id)
    except NotFound as exc:
        return _error_response(404, "not_found", str(exc))
    return JSONResponse(status_code=200, content={"run_id": run_id, "paths": paths})


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(auth.require_auth)])
async def cancel_run(run_id: str, db=Depends(get_db)) -> JSONResponse:
    try:
        run = runs_srv.request_cancel(db, run_id, actor="api")
    except NotFound as exc:
        return _error_response(404, "not_found", str(exc))
    except ConflictError as exc:
        return _error_response(409, "conflict", str(exc))
    return JSONResponse(status_code=200, content=_run_payload(run))


@router.post("/runs/{run_id}/resume", dependencies=[Depends(auth.require_auth)])
async def resume_run(run_id: str, db=Depends(get_db)) -> JSONResponse:
    try:
        run = runs_srv.resume(db, run_id, actor="api")
    except BusyError as exc:
        return JSONResponse(status_code=409, content=_busy_body(exc))
    except AlreadyDoneError as exc:
        return _error_response(409, "already_done", "该日期已有成功的运行,无需续跑", run_id=exc.run_id)
    except ConflictError as exc:
        return _error_response(409, "conflict", str(exc))
    except NotFound as exc:
        return _error_response(404, "not_found", str(exc))
    except ServiceValidationError as exc:
        return _error_response(400, "invalid_request", str(exc))
    return JSONResponse(status_code=200, content=_run_payload(run))
