"""财报原文获取:页面 `/reports`、htmx 片段 `/fragments/reports/*`、JSON API `/api/v1/report-jobs*`
与归档代理 `/api/v1/archive/*`(docs/REPORTS-FETCHER.md)。

- 任务提交/跟踪只写 SQLite(services.report_jobs),HTTP 由 app.reports.poller 线程执行。
- 归档查询与文件下载由后端代理到 reports-fetcher:浏览器不直连、令牌不下发前端;
  下载经 sha256/ETag 校验后再回给浏览器,支持浏览器 If-None-Match → 304。
- 本模块不含任何 mock 控制项(X-Mock-Scenario / /__mock/*)。
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import models
from app.reports import symbols
from app.reports.client import (
    ChecksumMismatch,
    ConnectionFailed,
    ProblemError,
    ReportsClient,
    RequestTimeout,
)
from app.services import instruments as inst_srv
from app.services import report_jobs as rj_srv
from app.services.errors import ConflictError, NotFound, ValidationError
from app.web import auth
from app.web.deps import get_current_actor, get_db, get_settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
page_router = APIRouter(tags=["reports"])
fragment_router = APIRouter(prefix="/fragments/reports", tags=["reports"])
api_router = APIRouter(prefix="/api/v1", tags=["reports-api"])

ARCHIVE_MARKETS = tuple(symbols.FETCHER_MARKET.values())


def _client(request: Request) -> ReportsClient | None:
    poller = getattr(request.app.state, "reports_poller", None)
    return getattr(poller, "client", None)


def _actor(request: Request) -> str:
    actor = getattr(request.state, "actor", "web")
    return actor if actor in ("web", "api") else "web"


def _err(status: int, error: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message, **extra})


def _upstream_error(exc: Exception) -> JSONResponse:
    """把上游错误映射成本系统错误体;不透传上游 request_id 以外的内部细节。"""
    if isinstance(exc, ProblemError):
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        return JSONResponse(
            status_code=502 if exc.status >= 500 else exc.status,
            content={
                "error": f"upstream_{exc.code}",
                "message": str(exc),
                "upstream_status": exc.status,
                "retryable": exc.retryable,
                "request_id": exc.request_id,
            },
            headers=headers,
        )
    if isinstance(exc, ChecksumMismatch):
        return _err(502, "checksum_mismatch", str(exc))
    if isinstance(exc, (ConnectionFailed, RequestTimeout)):
        return _err(503, "upstream_unreachable", f"财报服务不可达:{exc}")
    raise exc


# ================================================================== 视图数据
def _job_view(job: dict) -> dict:
    v = dict(job)
    prog = job.get("progress") or {}
    v["progress_text"] = (
        f"{prog.get('symbols_finished', 0)}/{prog.get('symbols_total', '?')}" if prog else "—"
    )
    summ = job.get("summary") or {}
    v["summary_text"] = (
        f"下载 {summ.get('downloaded', 0)} · 缓存 {summ.get('cached', 0)} · 失败 {summ.get('failed', 0)}"
        if summ
        else ""
    )
    v["report_count"] = len(job.get("report_ids") or [])
    return v


def _page_ctx(db, request: Request, *, message=None, error=None, code_prefill: str = "") -> dict:
    settings = request.app.state.settings
    poller = getattr(request.app.state, "reports_poller", None)
    jobs = rj_srv.list_jobs(db, limit=50)
    return {
        "enabled": settings.reports_enabled,
        "reports_health": poller.health() if poller else {"enabled": False, "alive": False},
        "instruments": inst_srv.list_instruments(db) if hasattr(inst_srv, "list_instruments") else [],
        "jobs": [_job_view(j) for j in jobs],
        "has_active": any(j["status"] in rj_srv.ACTIVE for j in jobs),
        "last_n_default": settings.reports_last_n_default,
        "max_last_n": rj_srv.MAX_LAST_N,
        "message": message,
        "error": error,
        "code_prefill": code_prefill,
        "archive_markets": ARCHIVE_MARKETS,
        "health": request.app.state.worker.health(),
    }


# ================================================================== 页面
@page_router.get("/reports", name="reports", dependencies=[Depends(auth.require_auth)])
async def reports_page(request: Request, db=Depends(get_db)):
    ctx = _page_ctx(db, request)
    ctx["placeholder"] = False
    return templates.TemplateResponse(request=request, name="reports.html", context=ctx)


@page_router.get(
    "/reports/jobs/{job_id}", name="report_job_detail", dependencies=[Depends(auth.require_auth)]
)
async def report_job_page(request: Request, job_id: str, db=Depends(get_db)):
    try:
        job = rj_srv.get(db, job_id)
    except NotFound:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "status": 404,
                "message": f"任务 {job_id} 不存在",
                "health": request.app.state.worker.health(),
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="report_job.html",
        context={"job": _job_view(job), "health": request.app.state.worker.health()},
    )


# ================================================================== 片段
@fragment_router.get("/jobs", dependencies=[Depends(auth.require_auth)])
async def jobs_fragment(request: Request, db=Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="fragments/report_jobs.html", context=_page_ctx(db, request)
    )


@fragment_router.post("/jobs", dependencies=[Depends(auth.require_auth)])
async def create_job_fragment(
    request: Request,
    db=Depends(get_db),
    settings=Depends(get_settings),
    code: str = Form(default=""),
    market: str = Form(default=""),
    last_n: int | None = Form(default=None),
    refresh: bool = Form(default=False),
):
    if not settings.reports_enabled:
        ctx = _page_ctx(db, request, error="未配置 REPORTS_API_BASE_URL,财报获取功能关闭")
        return templates.TemplateResponse(request=request, name="fragments/report_jobs.html", context=ctx)
    try:
        job = rj_srv.create(
            db,
            code=code,
            market=market or None,
            last_n=last_n,
            refresh=refresh,
            trigger="web",
            actor=_actor(request),
            default_last_n=settings.reports_last_n_default,
        )
        ctx = _page_ctx(db, request, message=f"已创建任务 {job['id']}:{job['code']} 最近 {job['last_n']} 期")
    except (ValidationError, ValueError, ConflictError) as exc:
        ctx = _page_ctx(db, request, error=str(exc), code_prefill=code)
    return templates.TemplateResponse(request=request, name="fragments/report_jobs.html", context=ctx)


@fragment_router.get("/jobs/{job_id}", dependencies=[Depends(auth.require_auth)])
async def job_detail_fragment(request: Request, job_id: str, db=Depends(get_db)):
    try:
        job = rj_srv.get(db, job_id)
    except NotFound:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request, name="fragments/report_job_detail.html", context={"job": _job_view(job)}
    )


@fragment_router.post("/jobs/{job_id}/refresh", dependencies=[Depends(auth.require_auth)])
async def job_refresh_fragment(request: Request, job_id: str, db=Depends(get_db)):
    try:
        job = rj_srv.reopen_for_refresh(db, job_id, actor=_actor(request))
        error = None
    except (NotFound, ConflictError) as exc:
        job = rj_srv.get(db, job_id)
        error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="fragments/report_job_detail.html",
        context={"job": _job_view(job), "error": error},
    )


@fragment_router.get("/archive", dependencies=[Depends(auth.require_auth)])
async def archive_fragment(
    request: Request,
    market: str = "",
    symbol: str = "",
    code: str = "",
    doc_type: str = "",
    cursor: str = "",
    limit: int = 20,
):
    """归档列表片段:market/symbol 直传上游;也可给本系统 code(自动换算 symbol)。"""
    client = _client(request)
    ctx: dict = {
        "items": [],
        "next_cursor": None,
        "market": market,
        "symbol": symbol,
        "code": code,
        "doc_type": doc_type,
        "error": None,
        "archive_markets": ARCHIVE_MARKETS,
    }
    if client is None:
        ctx["error"] = "未配置 REPORTS_API_BASE_URL"
        return templates.TemplateResponse(request=request, name="fragments/report_archive.html", context=ctx)
    try:
        if code:
            m, c = symbols.resolve(code)
            market = symbols.FETCHER_MARKET[m]
            symbol = symbols.archive_symbol(m, c)
            ctx.update(market=market, symbol=symbol)
        page = client.list_reports(
            market=market or None,
            symbol=symbol or None,
            doc_type=doc_type or None,
            limit=max(1, min(int(limit), 100)),
            cursor=cursor or None,
        )
        ctx["items"] = page.get("items") or []
        ctx["next_cursor"] = page.get("next_cursor")
    except ValueError as exc:
        ctx["error"] = str(exc)
    except (ProblemError, ConnectionFailed, RequestTimeout) as exc:
        ctx["error"] = f"归档查询失败:{exc}"
    return templates.TemplateResponse(request=request, name="fragments/report_archive.html", context=ctx)


# ================================================================== JSON API:任务
class ReportJobCreate(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    market: str | None = None
    last_n: int | None = Field(default=None, ge=1, le=rj_srv.MAX_LAST_N)
    refresh: bool = False


def _job_payload(job: dict) -> dict:
    keys = (
        "id",
        "instrument_id",
        "market",
        "code",
        "symbol",
        "last_n",
        "refresh",
        "remote_job_id",
        "status",
        "trigger",
        "submit_attempts",
        "progress",
        "summary",
        "results",
        "warnings",
        "report_ids",
        "error",
        "error_code",
        "error_retryable",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
    )
    return {k: job.get(k) for k in keys}


@api_router.post("/report-jobs", dependencies=[Depends(auth.require_auth)])
async def api_create_job(
    body: ReportJobCreate, request: Request, db=Depends(get_db), settings=Depends(get_settings)
):
    if not settings.reports_enabled:
        return _err(503, "reports_disabled", "未配置 REPORTS_API_BASE_URL")
    try:
        job = rj_srv.create(
            db,
            code=body.code,
            market=body.market,
            last_n=body.last_n,
            refresh=body.refresh,
            trigger="api",
            actor="api",
            default_last_n=settings.reports_last_n_default,
        )
    except (ValidationError, ValueError) as exc:
        return _err(400, "invalid_request", str(exc))
    except ConflictError as exc:
        return _err(409, "conflict", str(exc))
    return JSONResponse(status_code=202, content=_job_payload(job))


@api_router.get("/report-jobs", dependencies=[Depends(auth.require_auth)])
async def api_list_jobs(
    db=Depends(get_db),
    status: str | None = None,
    code: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    return [_job_payload(j) for j in rj_srv.list_jobs(db, status=status, code=code, limit=limit)]


@api_router.get("/report-jobs/{job_id}", dependencies=[Depends(auth.require_auth)])
async def api_get_job(job_id: str, db=Depends(get_db)):
    try:
        return _job_payload(rj_srv.get(db, job_id))
    except NotFound as exc:
        return _err(404, "not_found", str(exc))


@api_router.post("/report-jobs/{job_id}/refresh", dependencies=[Depends(auth.require_auth)])
async def api_refresh_job(job_id: str, db=Depends(get_db), actor: str = Depends(get_current_actor)):
    try:
        return _job_payload(rj_srv.reopen_for_refresh(db, job_id, actor="api"))
    except NotFound as exc:
        return _err(404, "not_found", str(exc))
    except ConflictError as exc:
        return _err(409, "conflict", str(exc))


# ================================================================== JSON API:归档代理
@api_router.get("/archive/reports", dependencies=[Depends(auth.require_auth)])
async def api_archive_list(
    request: Request,
    market: str | None = None,
    symbol: str | None = None,
    code: str | None = None,
    doc_type: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
):
    client = _client(request)
    if client is None:
        return _err(503, "reports_disabled", "未配置 REPORTS_API_BASE_URL")
    try:
        if code:
            m, c = symbols.resolve(code)
            market, symbol = symbols.FETCHER_MARKET[m], symbols.archive_symbol(m, c)
        return client.list_reports(
            market=market,
            symbol=symbol,
            doc_type=doc_type,
            period_from=period_from,
            period_to=period_to,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        return _err(400, "invalid_request", str(exc))
    except (ProblemError, ConnectionFailed, RequestTimeout, ChecksumMismatch) as exc:
        return _upstream_error(exc)


@api_router.get("/archive/reports/{report_id}", dependencies=[Depends(auth.require_auth)])
async def api_archive_detail(request: Request, report_id: str):
    client = _client(request)
    if client is None:
        return _err(503, "reports_disabled", "未配置 REPORTS_API_BASE_URL")
    try:
        return client.get_report(report_id)
    except (ProblemError, ConnectionFailed, RequestTimeout, ChecksumMismatch) as exc:
        return _upstream_error(exc)


@api_router.get("/archive/reports/{report_id}/file", dependencies=[Depends(auth.require_auth)])
async def api_archive_file(request: Request, report_id: str, artifact_id: str | None = None):
    """代理下载:上游 ETag(sha256)校验通过后回给浏览器;浏览器带 If-None-Match 命中时透传 304。"""
    client = _client(request)
    if client is None:
        return _err(503, "reports_disabled", "未配置 REPORTS_API_BASE_URL")
    inm = request.headers.get("if-none-match")
    try:
        result = client.download_report_file(report_id, artifact_id=artifact_id, if_none_match=inm)
    except (ProblemError, ConnectionFailed, RequestTimeout, ChecksumMismatch) as exc:
        return _upstream_error(exc)
    if result["status"] == 304:
        return Response(status_code=304, headers={"ETag": result["etag"] or ""})
    upstream_name = result["filename"]
    meta: dict = {"report_id": report_id}
    if not _is_readable_upstream_name(upstream_name):
        try:
            meta = client.get_report(report_id)
        except (ProblemError, ConnectionFailed, RequestTimeout):
            meta = {"report_id": report_id}
    filename = _build_filename(meta, upstream_name, result["media_type"])
    disposition = (
        f'attachment; filename="{_ascii_filename(filename)}"; '
        f"filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"
    )
    headers = {
        "ETag": result["etag"] or f'"{result["sha256"]}"',
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "X-Checksum-SHA256": result["sha256"],
        "Cache-Control": "private, no-cache",
    }
    return Response(
        content=result["content"],
        media_type=result["media_type"] or "application/octet-stream",
        headers=headers,
    )


_MEDIA_TYPE_EXT = {"application/pdf": "pdf", "text/html": "html"}
_SAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]")


def _is_readable_upstream_name(name: str | None) -> bool:
    """上游名可读:非空、不以 `unknown__` 开头、去扩展名后含字母数字或中文。"""
    if not name or name.startswith("unknown__"):
        return False
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return re.search(r"[0-9A-Za-z\u4e00-\u9fff]", stem) is not None


def _ext_from_media_type(media_type: str | None) -> str:
    base = (media_type or "").split(";", 1)[0].strip().lower()
    return _MEDIA_TYPE_EXT.get(base, "bin")


def _local_code(market: str | None, symbol: str | None) -> str | None:
    """上游规范化 symbol → 本系统代码形式(港股 `00700`→`0700.HK`);失败用上游原样。"""
    if not symbol:
        return None
    local = symbols.LOCAL_MARKET.get((market or "").upper())
    if not local:
        return symbol
    try:
        if local == "hk":
            return models.normalize_code("hk", f"{int(symbol.split('.', 1)[0])}.HK")
        return models.normalize_code(local, symbol)
    except (ValueError, TypeError):
        return symbol


def _build_filename(report: dict | None, upstream_name: str | None, media_type: str | None) -> str:
    """可读上游名原样(经清洗)使用;否则组装 `<market>_<code>_<doc_type>_<date>_<report_id>.<ext>`。"""
    ext = _ext_from_media_type(media_type)
    if _is_readable_upstream_name(upstream_name):
        return _sanitize_filename(upstream_name) or f"report.{ext}"
    meta = report or {}
    segments = (
        meta.get("market"),
        _local_code(meta.get("market"), meta.get("symbol")),
        meta.get("doc_type"),
        meta.get("report_period") or meta.get("filing_date"),
        meta.get("report_id"),
    )
    stem = "_".join(str(s) for s in segments if s) or "report"
    return _sanitize_filename(f"{stem}.{ext}")


def _sanitize_filename(name: str, *, limit: int = 150) -> str:
    return _SAFE_FILENAME_RE.sub("_", name)[:limit]


def _ascii_filename(name: str) -> str:
    safe = "".join(ch if ch.isascii() and ch.isprintable() and ch not in '"\\/' else "_" for ch in name)
    safe = re.sub(r"_{2,}", "_", safe)
    return safe or "report.bin"


def register(app) -> None:
    app.include_router(page_router)
    app.include_router(fragment_router)
    app.include_router(api_router)


__all__ = ["register", "page_router", "fragment_router", "api_router", "models"]
