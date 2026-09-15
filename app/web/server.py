"""create_app()、lifespan、异常边界、/healthz(DESIGN §4.10 / R-FND-08/09)。

Worker 与 Scheduler 在 P0 阶段为 stub(DESIGN §4.8/§4.9 接口面),
通过 create_app(..., worker=..., scheduler=...) 注入,S3/S4 替换为真实实现。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from sqlite3 import Connection

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, models
from app.audit import scrub
from app.config import Settings
from app.executor.worker import Worker
from app.scheduler import Scheduler
from app.web import auth
from app.web.routes import pages

log = logging.getLogger("am.server")

_WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def create_app(
    settings: Settings | None = None,
    *,
    worker: Worker | None = None,
    scheduler: Scheduler | None = None,
    db_factory: Callable[[], Connection] | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    db_factory = db_factory or (lambda: db.connect(settings.db_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        conn = db_factory()
        try:
            db.migrate(conn)
        finally:
            conn.close()
        app.state.scheduler.start()
        app.state.worker.start()
        log.info("agents-manage 启动完成,配置:%s", scrub(settings.summary()))
        yield
        app.state.scheduler.shutdown()
        app.state.worker.stop()
        log.info("agents-manage 已停止(worker 与 scheduler;执行容器不受影响)")

    app = FastAPI(title="Agents-Manage", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.worker = worker or Worker(settings, launcher=None, db_factory=db_factory)
    app.state.scheduler = scheduler or Scheduler(settings, db_factory)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
    app.include_router(pages.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request, _: None = Depends(auth.require_auth)) -> JSONResponse:
        """R-FND-09:worker alive、queue depth、docker reachable、scheduler jobs、当前 run。"""
        health = app.state.worker.health()
        return JSONResponse(
            {
                "status": "ok",
                "worker_alive": health["alive"],
                "queue_depth": health["queue_depth"],
                "docker_ok": health["docker_ok"],
                "scheduler_jobs": app.state.scheduler.jobs_count,
                "current_run_id": health["current_run_id"],
                "time": models.now_sh().isoformat(timespec="seconds"),
            }
        )

    def _wants_json(request: Request) -> bool:
        return request.url.path.startswith("/api/") or request.url.path == "/healthz"

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "message": "请求参数非法",
                    "detail": str(exc),
                },
            )
        return JSONResponse(status_code=400, content={"error": "invalid_request", "message": "请求参数非法"})

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException):
        if exc.status_code == 302 and "Location" in (exc.headers or {}):
            from fastapi.responses import RedirectResponse

            return RedirectResponse(exc.headers["Location"], status_code=302)
        detail = (
            exc.detail if isinstance(exc.detail, dict) else {"error": "error", "message": str(exc.detail)}
        )
        return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception):
        """全局异常边界:handler 异常不影响 worker(线程隔离,DESIGN §2)。"""
        log.exception("未处理异常 path=%s", request.url.path, extra={"err": scrub(str(exc))})
        if _wants_json(request):
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal",
                    "message": "服务器内部错误",
                },
            )
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"status": 500, "message": "服务器内部错误"},
            status_code=500,
        )

    return app
