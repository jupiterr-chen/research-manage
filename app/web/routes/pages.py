"""页面路由(P0 骨架):/login、/、/instruments 占位。

总览、标的与调度、运行详情的完整页面在 feat/web-ui(S6)交付;
此处提供登录闭环(R-WEB-07)与最小壳页,保证 /healthz、/login 可访问(DoD)。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web import auth
from app.web.deps import get_current_actor

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
async def overview(request: Request, actor: str = Depends(get_current_actor)):
    health = request.app.state.worker.health()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"actor": actor, "health": health, "placeholder": True},
    )


@router.get("/instruments", name="instruments", dependencies=[Depends(auth.require_auth)])
async def instruments_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="instruments.html",
        context={"health": request.app.state.worker.health(), "placeholder": True},
    )
