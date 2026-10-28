"""FastAPI 应用：Web 工作台 + SSE 流式事件 + 运行/报告查询接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from opspilot import __version__
from opspilot.config import get_settings
from opspilot.core.loop import AgentLoop
from opspilot.demo_scripts import mock_for
from opspilot.harness.state import StateStore
from opspilot.harness.tracing import Tracer
from opspilot.llm import build_llm
from opspilot.security.access import require_run, require_session
from opspilot.security.audit import AuditLog
from opspilot.security.auth import (
    COOKIE_NAME,
    SESSION_TTL_S,
    authenticate,
    current_principal,
    sign_session,
)
from opspilot.security.model import ROLES, Principal
from opspilot.modules.dashboard.analytics import dashboard_overview
from opspilot.modules.dashboard.data_version import data_signature
from opspilot.modules.metrics.service import compute_metrics
from opspilot.modules.reporting.service import dedupe_reports, report_to_markdown

WEB_DIR = Path(__file__).resolve().parents[1] / "frontend"
settings = get_settings()
logger = logging.getLogger("opspilot.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = StateStore(settings.state_db_path)
    app.state.audit = AuditLog(settings.runs_dir)
    recovered = app.state.store.mark_interrupted_runs()
    if recovered:
        logger.warning("发现 %d 个中断运行，已标记为 interrupted", recovered)
    logger.info("OpsPilot %s 启动，模型 %s", __version__, settings.llm_model)
    yield
    app.state.store.close()


app = FastAPI(title="OpsPilot", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """SameSite=Lax 之外再挡一层跨站 POST；演示级防护，不是完整 CSRF 方案。"""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin and origin not in settings.allowed_origin_list:
            return JSONResponse({"detail": "Origin 不被允许"}, status_code=403)
    return await call_next(request)


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, description="自然语言调查任务")
    session_id: str | None = None
    mock: bool = Field(default=False, description="无 API Key 时用脚本化模型演示")


class ResumeRequest(BaseModel):
    mock: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str


# --------------------------------------------------------------------- 认证

@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    user = authenticate(req.username, req.password)
    if not user:
        app.state.audit.record(
            action="auth.login",
            decision="deny",
            user_id=req.username,
            role=None,
            reason="BAD_CREDENTIALS",
        )
        raise HTTPException(401, "用户名或密码错误")
    token = sign_session(user.user_id)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=SESSION_TTL_S,
        path="/",
    )
    app.state.audit.record(
        action="auth.login",
        decision="allow",
        user_id=user.user_id,
        role=user.role,
    )
    return {"ok": True}


@app.get("/api/auth/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    role = ROLES[principal.role]
    return {
        "user": {
            "id": principal.user_id,
            "name": principal.display_name,
            "role": principal.role,
            "role_label": role.label,
        },
        "scope": {
            "workshops": principal.as_scope(),
            "label": principal.scope_label,
        },
        "tools": sorted(role.tools),
        "can_read_audit": role.can_read_audit,
    }


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# --------------------------------------------------------------------- 页面

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    db_ok = settings.db_path.exists()
    return {
        "status": "ok",
        "version": __version__,
        "model": settings.llm_model,
        "llm_configured": settings.has_llm,
        "database": {"path": str(settings.db_path), "ready": db_ok},
        "business_api": settings.business_api_base,
        "budget": {
            "max_steps": settings.max_steps,
            "max_tool_calls": settings.max_tool_calls,
            "max_tokens": settings.max_tokens,
            "max_seconds": settings.max_seconds,
        },
    }


@app.get("/api/tools")
async def tools(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from opspilot.harness.registry import build_default_registry

    registry = build_default_registry()
    names = registry.names_for(principal)
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "read_only": spec.read_only,
                "tags": list(spec.tags),
                "parameters": spec.parameters_schema(),
            }
            for spec in registry.specs(names)
        ]
    }


def _dashboard_payload(
    days: int, *, reason: str = "snapshot", workshop_id: str | None = None
) -> dict[str, Any]:
    sig = data_signature(settings.db_path)
    # DuckDB 是单写多读：外部写入时可能短暂持有排他锁，读侧重试几次即可
    last: Exception | None = None
    for attempt in range(4):
        try:
            data = dashboard_overview(settings.db_path, days=days, workshop_id=workshop_id)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.25 * (attempt + 1))
    else:
        raise last  # type: ignore[misc]
    return {
        **data,
        "data_version": sig.version,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def dashboard_event_stream(
    request: Request,
    days: int = 30,
    poll_s: float = 2.0,
    workshop_id: str | None = None,
):
    """数据变更推送：轮询文件指纹，变化时重算并推一份新的看板快照。

    指纹相同则不重算、不推送；每 15 秒发一次注释行保活。
    """
    last_version: str | None = None
    last_sent = 0.0
    while True:
        if await request.is_disconnected():
            break
        try:
            sig = data_signature(settings.db_path)
            if sig.version != last_version:
                # 防抖：写入可能还在进行，400ms 后指纹仍变就下一轮再算
                await asyncio.sleep(0.4)
                if data_signature(settings.db_path).version != sig.version:
                    await asyncio.sleep(poll_s)
                    continue
                payload = await asyncio.to_thread(
                    _dashboard_payload,
                    days,
                    reason="changed" if last_version else "initial",
                    workshop_id=workshop_id,
                )
                yield _sse("dashboard", payload)
                last_version = payload["data_version"]
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= 15:
                yield ": ping\n\n"
                last_sent = time.monotonic()
        except FileNotFoundError:
            yield _sse("stream_error", {"message": "数据库文件不存在，请先运行 python -m opspilot.data.seed"})
            last_version = None
        except Exception as exc:  # noqa: BLE001 - 单次失败不应中断推送
            yield _sse("stream_error", {"message": f"{type(exc).__name__}: {exc}"[:300]})
            last_version = None
        await asyncio.sleep(poll_s)


@app.get("/api/dashboard")
async def dashboard(
    days: int = Query(default=30, ge=7, le=92),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    workshop_id = next(iter(principal.workshops)) if principal.workshops else None
    return await asyncio.to_thread(_dashboard_payload, days, workshop_id=workshop_id)


@app.get("/api/dashboard/stream")
async def dashboard_stream(
    request: Request,
    days: int = Query(default=30, ge=7, le=92),
    principal: Principal = Depends(current_principal),
):
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    workshop_id = next(iter(principal.workshops)) if principal.workshops else None
    return StreamingResponse(
        dashboard_event_stream(request, days, workshop_id=workshop_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------- 运行

@app.get("/api/metrics")
async def metrics(
    days: int = Query(default=30, ge=0, le=365),
    principal: Principal = Depends(current_principal),
):
    """运行统计：状态、耗时、token、工具调用与错误分布。days=0 表示全部。"""
    since = time.time() - days * 86400 if days else 0.0
    owner = None if principal.role == "executive" else principal.user_id
    rows = app.state.store.runs_since(since, limit=500, owner_user_id=owner)
    return await asyncio.to_thread(compute_metrics, rows, settings.runs_dir, days=days)


@app.get("/api/runs")
async def list_runs(
    session_id: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    rows = app.state.store.list_runs(
        session_id=session_id, limit=limit, owner_user_id=principal.user_id
    )
    return {"runs": rows}


@app.get("/api/sessions")
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    return {
        "sessions": app.state.store.list_sessions(
            limit=limit, owner_user_id=principal.user_id
        )
    }


@app.get("/api/sessions/{session_id}")
async def session_detail(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    runs = require_session(app.state.store, session_id, principal)[:limit]
    return {"session_id": session_id, "runs": runs}


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str, principal: Principal = Depends(current_principal)
):
    """删除对话：普通问答与文件清掉，产出过报告的运行连同 Trace 一起保留。"""
    require_session(app.state.store, session_id, principal)
    result = app.state.store.archive_session(
        session_id, owner_user_id=principal.user_id
    )
    if not result:
        raise HTTPException(404, "会话不存在")
    removed_dirs = 0
    for run_id in result["removed"]:
        path = settings.runs_dir / run_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1
    app.state.audit.record(
        action="session.delete",
        decision="allow",
        user_id=principal.user_id,
        role=principal.role,
        detail={"session_id": session_id, "removed": len(result["removed"])},
    )
    return {
        "deleted": True,
        "session_id": session_id,
        "kept_reports": result["kept"],
        "removed_runs": result["removed"],
        "removed_dirs": removed_dirs,
    }


@app.get("/api/reports")
async def list_reports(
    limit: int = Query(default=50, ge=1, le=200),
    dedupe: bool = True,
    principal: Principal = Depends(current_principal),
):
    rows = app.state.store.list_reports(
        limit=200, owner_user_id=principal.user_id
    )
    if not dedupe:
        return {"reports": rows[:limit]}
    return {"reports": dedupe_reports(rows, limit=limit)}


@app.get("/api/runs/{run_id}")
async def get_run(
    run_id: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    row = require_run(app.state.store, run_id, principal)
    return {"run": row}


@app.get("/api/runs/{run_id}/events")
async def get_events(
    run_id: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    require_run(app.state.store, run_id, principal)
    return {"events": Tracer.load_events(run_id, settings.runs_dir)}


@app.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
async def export_report(
    run_id: str, principal: Principal = Depends(current_principal)
) -> PlainTextResponse:
    row = require_run(app.state.store, run_id, principal)
    report = row.get("report")
    if not report:
        raise HTTPException(404, "该运行没有结构化报告")
    meta = {
        "run_id": run_id,
        "steps": row.get("step"),
        "usage": row.get("usage"),
        "finished_at": datetime.fromtimestamp(row.get("updated_at") or 0).isoformat(timespec="seconds"),
    }
    md = report_to_markdown(report, run_meta=meta)
    app.state.audit.record(
        action="report.export",
        decision="allow",
        user_id=principal.user_id,
        role=principal.role,
        run_id=run_id,
    )
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-report.md"'},
    )


@app.post("/api/chat")
async def chat(req: ChatRequest, principal: Principal = Depends(current_principal)):
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    if not req.mock and not settings.has_llm:
        raise HTTPException(400, "未配置 OPSPILOT_LLM_API_KEY；可在界面上选择离线演示模式")

    if req.mock:
        history = (
            app.state.store.session_history(
                req.session_id, limit=3, owner_user_id=principal.user_id
            )
            if req.session_id
            else []
        )
        llm = mock_for(req.message, [h.get("task") or "" for h in history])
    else:
        llm = build_llm(settings)
    loop = AgentLoop(
        llm, settings=settings, state=app.state.store, audit=app.state.audit
    )
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_event(event) -> None:
        queue.put_nowait(event.to_dict())

    async def runner() -> None:
        try:
            await loop.run(
                req.message,
                principal=principal,
                session_id=req.session_id,
                emit=on_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            queue.put_nowait({"type": "error", "run_id": "", "seq": 0, "data": {"message": str(exc)}})
        finally:
            queue.put_nowait(None)
            await llm.aclose()

    task = asyncio.create_task(runner())

    async def stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
            yield 'data: {"type": "__end__"}\n\n'
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/{run_id}/resume")
async def resume_run(
    run_id: str, req: ResumeRequest, principal: Principal = Depends(current_principal)
):
    row = require_run(app.state.store, run_id, principal)
    if req.mock:
        history = (
            app.state.store.session_history(
                row.get("session_id") or "",
                exclude_run_id=run_id,
                limit=3,
                owner_user_id=principal.user_id,
            )
            if row.get("session_id")
            else []
        )
        llm = mock_for(row.get("task") or "", [h.get("task") or "" for h in history])
    else:
        llm = build_llm(settings)
    loop = AgentLoop(
        llm, settings=settings, state=app.state.store, audit=app.state.audit
    )
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_event(event) -> None:
        queue.put_nowait(event.to_dict())

    async def runner() -> None:
        try:
            await loop.resume(run_id, principal=principal, emit=on_event)
        except Exception as exc:  # noqa: BLE001
            queue.put_nowait({"type": "error", "run_id": run_id, "seq": 0, "data": {"message": str(exc)}})
        finally:
            queue.put_nowait(None)
            await llm.aclose()

    task = asyncio.create_task(runner())

    async def stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
            yield 'data: {"type": "__end__"}\n\n'
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------- 审计 / 产物

@app.get("/api/audit")
async def audit_entries(
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    if not ROLES[principal.role].can_read_audit:
        raise HTTPException(403, "无权查看审计")
    return {"entries": app.state.audit.recent(limit)}


@app.get("/artifacts/{run_id}/{path:path}")
async def artifact(
    run_id: str, path: str, principal: Principal = Depends(current_principal)
):
    """受控产物读取：先校验 run owner，再限制后缀与路径。"""
    require_run(app.state.store, run_id, principal)
    base = (settings.runs_dir / run_id / "artifacts").resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404)
    suffix = target.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".txt", ".csv", ".json", ".md"}:
        raise HTTPException(404)
    inline = suffix in {".png", ".jpg", ".jpeg", ".webp"}
    return FileResponse(
        target,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline" if inline else "attachment",
            "Cache-Control": "private, max-age=60",
        },
    )


# --------------------------------------------------------------------- 静态资源

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

settings.runs_dir.mkdir(parents=True, exist_ok=True)


def main() -> None:
    import uvicorn

    uvicorn.run("opspilot.api.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
