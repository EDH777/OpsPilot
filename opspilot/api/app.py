"""FastAPI 应用：Web 工作台 + SSE 流式事件 + 运行/报告查询接口。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
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
from opspilot.llm.base import LLMError
from opspilot.services.analytics import dashboard_overview
from opspilot.services.data_version import data_signature
from opspilot.services.reporting import dedupe_reports, report_to_markdown

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = StateStore(settings.state_db_path)
    yield
    app.state.store.close()


app = FastAPI(title="OpsPilot", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")] if settings.cors_origins else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, description="自然语言调查任务")
    session_id: str | None = None
    mock: bool = Field(default=False, description="无 API Key 时用脚本化模型演示")


class ResumeRequest(BaseModel):
    mock: bool = False


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
async def tools() -> dict[str, Any]:
    from opspilot.harness.registry import build_default_registry

    registry = build_default_registry()
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "read_only": spec.read_only,
                "tags": list(spec.tags),
                "parameters": spec.parameters_schema(),
            }
            for spec in registry.specs()
        ]
    }


def _dashboard_payload(days: int, *, reason: str = "snapshot") -> dict[str, Any]:
    sig = data_signature(settings.db_path)
    # DuckDB 是单写多读：外部写入时可能短暂持有排他锁，读侧重试几次即可
    last: Exception | None = None
    for attempt in range(4):
        try:
            data = dashboard_overview(settings.db_path, days=days)
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


async def dashboard_event_stream(request: Request, days: int = 30, poll_s: float = 2.0):
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
                    _dashboard_payload, days, reason="changed" if last_version else "initial"
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
async def dashboard(days: int = Query(default=30, ge=7, le=92)) -> dict[str, Any]:
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    return await asyncio.to_thread(_dashboard_payload, days)


@app.get("/api/dashboard/stream")
async def dashboard_stream(request: Request, days: int = Query(default=30, ge=7, le=92)):
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    return StreamingResponse(
        dashboard_event_stream(request, days),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------- 运行

@app.get("/api/runs")
async def list_runs(session_id: str | None = None, limit: int = Query(default=30, ge=1, le=100)):
    rows = app.state.store.list_runs(session_id=session_id, limit=limit)
    return {"runs": rows}


@app.get("/api/sessions")
async def list_sessions(limit: int = Query(default=20, ge=1, le=100)):
    return {"sessions": app.state.store.list_sessions(limit=limit)}


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, limit: int = Query(default=200, ge=1, le=500)):
    runs = app.state.store.session_runs(session_id, limit=limit)
    if not runs:
        raise HTTPException(404, "会话不存在")
    return {"session_id": session_id, "runs": runs}


@app.get("/api/reports")
async def list_reports(limit: int = Query(default=50, ge=1, le=200), dedupe: bool = True):
    rows = app.state.store.list_reports(limit=200)
    if not dedupe:
        return {"reports": rows[:limit]}
    return {"reports": dedupe_reports(rows, limit=limit)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    row = app.state.store.load(run_id)
    if not row:
        raise HTTPException(404, "run 不存在")
    return {"run": row}


@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: str) -> dict[str, Any]:
    return {"events": Tracer.load_events(run_id, settings.runs_dir)}


@app.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
async def export_report(run_id: str) -> PlainTextResponse:
    row = app.state.store.load(run_id)
    if not row:
        raise HTTPException(404, "run 不存在")
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
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-report.md"'},
    )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not settings.db_path.exists():
        raise HTTPException(503, "数据库尚未生成，请先运行 python -m opspilot.data.seed")
    if not req.mock and not settings.has_llm:
        raise HTTPException(400, "未配置 OPSPILOT_LLM_API_KEY；可在界面上选择离线演示模式")

    llm = mock_for(req.message) if req.mock else build_llm(settings)
    loop = AgentLoop(llm, settings=settings, state=app.state.store)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_event(event) -> None:
        queue.put_nowait(event.to_dict())

    async def runner() -> None:
        try:
            await loop.run(req.message, session_id=req.session_id, emit=on_event)
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
async def resume_run(run_id: str, req: ResumeRequest):
    row = app.state.store.load(run_id)
    if not row:
        raise HTTPException(404, "run 不存在")
    llm = mock_for(row.get("task") or "") if req.mock else build_llm(settings)
    loop = AgentLoop(llm, settings=settings, state=app.state.store)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_event(event) -> None:
        queue.put_nowait(event.to_dict())

    async def runner() -> None:
        try:
            await loop.resume(run_id, emit=on_event)
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


# --------------------------------------------------------------------- 静态资源

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

settings.runs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=str(settings.runs_dir)), name="artifacts")


def main() -> None:
    import uvicorn

    uvicorn.run("opspilot.api.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
