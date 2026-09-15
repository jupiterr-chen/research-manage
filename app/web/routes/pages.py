"""页面路由(SPEC §7 / R-WEB-01~08):总览、标的与调度、运行详情、登录。"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import models
from app.services import profiles as prof_srv
from app.services import schedules as sch_srv
from app.web import auth
from app.web.deps import get_current_actor, get_db
from app.web.routes.fragments import _instrument_block_ctx, _overview_ctx, _run_detail_ctx

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _safe_next(raw: str | None) -> str:
    """只允许站内路径,防开放重定向。"""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login", name="login")
async def login_page(request: Request, next: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next": _safe_next(next), "error": None, "local_mode": not request.app.state.settings.token},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    token: str = Form(default=""),
    next: str = Form(default=""),
):
    ip = request.client.host if request.client else "unknown"
    settings = request.app.state.settings
    if auth.login_rate_limited(ip):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=429,
            context={
                "next": _safe_next(next),
                "error": "尝试过于频繁,请 1 分钟后再试",
                "local_mode": not settings.token,
            },
        )
    if not settings.token or not token or not secrets.compare_digest(settings.token, token):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=401,
            context={
                "next": _safe_next(next),
                "error": auth.LOGIN_FAIL_MESSAGE,
                "local_mode": not settings.token,
            },
        )
    response = RedirectResponse(_safe_next(next) or "/", status_code=303)
    auth.set_session_cookie(response, settings)
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response


@router.get("/", name="overview", dependencies=[Depends(auth.require_auth)])
async def overview_page(request: Request, db=Depends(get_db), actor: str = Depends(get_current_actor)):
    """总览壳:手动发起卡(静态)+ 首屏数据(轮询片段承载)。"""
    ctx = _overview_ctx(db, request)
    ctx.update(
        {
            "actor": actor,
            "health": request.app.state.worker.health(),
            "profiles": prof_srv.list_profiles(db),
            "analysts_options": models.ANALYSTS,
            "analyst_label": models.ANALYST_LABEL,
            "today": models.today_sh().isoformat(),
            "placeholder": False,
        }
    )
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/instruments", name="instruments", dependencies=[Depends(auth.require_auth)])
async def instruments_page(request: Request, db=Depends(get_db)):
    ctx = _instrument_block_ctx(db, request)
    ctx.update(
        {
            "health": request.app.state.worker.health(),
            "placeholder": False,
            "kind_label": sch_srv.KIND_LABEL,
            "weekday_label": sch_srv.WEEKDAY_LABEL,
        }
    )
    return templates.TemplateResponse(request=request, name="instruments.html", context=ctx)


@router.get("/runs/{run_id}", name="run_detail", dependencies=[Depends(auth.require_auth)])
async def run_detail_page(request: Request, run_id: str, db=Depends(get_db)):
    ctx = _run_detail_ctx(db, request, run_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"run {run_id} 不存在"})
    ctx.update({"health": request.app.state.worker.health()})
    return templates.TemplateResponse(request=request, name="runs_detail.html", context=ctx)
