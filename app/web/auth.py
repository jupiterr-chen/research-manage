"""鉴权:token/cookie/登录限速(DESIGN §4.11 / R-FND-04/05)。

- `Authorization: Bearer <token>`(actor=api)或 cookie `am_session`(actor=web)
- cookie 值 = hmac(token, "session") 十六进制,不存 token 本身
- 登录限速:内存 {ip: deque[timestamps]},5 次/60s,超限 429
- 失败文案不区分「token 错」与「未设置」
- AM_TOKEN 未配置(仅限回环绑定,config 门禁保证)→ 本地模式直接放行
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "am_session"
SESSION_MESSAGE = "session"
LOGIN_FAIL_MESSAGE = "登录失败:token 不正确或未设置"
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 60.0

_login_attempts: dict[str, deque[float]] = {}


def session_value(token: str) -> str:
    return hmac.new(token.encode(), SESSION_MESSAGE.encode(), hashlib.sha256).hexdigest()


def _check_token(settings, presented: str) -> bool:
    return secrets.compare_digest(settings.token, presented)


def _cookie_ok(request: Request, settings) -> bool:
    raw = request.cookies.get(COOKIE_NAME)
    return bool(raw) and secrets.compare_digest(session_value(settings.token), raw)


def _bearer_ok(request: Request, settings) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    return _check_token(settings, header[7:].strip())


def set_session_cookie(response: Response, settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        session_value(settings.token),
        httponly=True,
        samesite="strict",
        secure=False,  # 内网 http 部署;https 由反向代理提供时无法自判,保持可配边界简单
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def check_auth(request: Request, settings) -> str | None:
    """通过 → 'api'(Bearer)或 'web'(cookie);未通过 → None。本地模式(token 未配置)→ 'local'。"""
    if not settings.token:
        return "local"
    if _bearer_ok(request, settings):
        return "api"
    if _cookie_ok(request, settings):
        return "web"
    return None


def require_auth(request: Request) -> None:
    """FastAPI 依赖:API 路径 401 JSON,页面路径 302 /login?next=。

    带了 Authorization 头但校验失败 → 无论路径一律 401(明确是鉴权失败,不是未登录)。
    """
    has_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    actor = check_auth(request, request.app.state.settings)
    if actor is not None:
        request.state.actor = actor
        return
    if has_bearer or request.url.path.startswith("/api/") or request.url.path == "/healthz":
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "未鉴权"})
    raise HTTPException(
        status_code=302,
        detail="redirect to login",
        headers={"Location": f"/login?next={request.url.path}"},
    )


def login_rate_limited(ip: str) -> bool:
    """同 IP 60s 窗口内第 6 次尝试 → True;顺带清理过期项。"""
    now = time.monotonic()
    bucket = _login_attempts.setdefault(ip, deque())
    while bucket and now - bucket[0] > RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        return True
    bucket.append(now)
    return False
