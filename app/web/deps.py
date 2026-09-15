"""依赖注入:db、settings、current_actor(DESIGN §1)。"""

from __future__ import annotations

from collections.abc import Iterator
from sqlite3 import Connection

from fastapi import Request

from app.config import Settings
from app.db import connect


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Connection]:
    """每请求一个连接(Web 层);Worker/Scheduler 线程自建连接,不共用。"""
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_current_actor(request: Request) -> str:
    """require_auth 已写入 request.state.actor;web/api/local。"""
    return getattr(request.state, "actor", "web")
