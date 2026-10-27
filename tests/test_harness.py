"""Harness 单元测试：参数修复、错误分类、预算、上下文、状态、追踪。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from opspilot.core.types import Message, Role, ToolCall, Usage
from opspilot.harness.budget import Budget, BudgetExceeded
from opspilot.harness.context import ContextManager
from opspilot.harness.errors import ErrorKind, ToolError, classify_exception
from opspilot.harness.executor import ToolExecutor, repair_arguments
from opspilot.harness.registry import ToolContext, ToolRegistry, ToolSpec
from opspilot.harness.state import StateStore
from opspilot.harness.tracing import Tracer
from opspilot.core.events import Event


# ---------------------------------------------------------------- 参数修复

class QueryArgs(BaseModel):
    workshop_id: str
    start_date: str
    limit: int = 100
    types: list[str] = Field(default_factory=list)


def test_repair_normalizes_dates_types_and_lists():
    fixed, notes = repair_arguments(
        {
            "workshop_id": "WS-03",
            "start_date": "2026/9/1",
            "limit": "50",
            "types": "空压机,风机",
            "unknown": "x",
        },
        QueryArgs,
    )
    assert fixed["start_date"] == "2026-09-01"
    assert fixed["limit"] == 50
    assert fixed["types"] == ["空压机", "风机"]
    assert "unknown" not in fixed
    assert any("start_date" in n for n in notes)
    assert any("unknown" in n for n in notes)


def test_repair_strips_iso_time_from_date_field():
    fixed, _ = repair_arguments({"workshop_id": "WS-03", "start_date": "2026-09-01T00:00:00+08:00"}, QueryArgs)
    assert fixed["start_date"] == "2026-09-01"


def test_repair_month_field():
    class MonthArgs(BaseModel):
        month: str

    fixed, _ = repair_arguments({"month": "2026/9"}, MonthArgs)
    assert fixed["month"] == "2026-09"


# ---------------------------------------------------------------- 错误分类

def test_classify_common_exceptions():
    assert classify_exception(TimeoutError()).kind is ErrorKind.TIMEOUT
    assert classify_exception(ValueError("bad")).kind is ErrorKind.INVALID_ARGUMENT
    assert classify_exception(PermissionError("denied")).kind is ErrorKind.PERMISSION_DENIED
    assert classify_exception(KeyError("WS-99")).kind is ErrorKind.NOT_FOUND
    err = classify_exception(RuntimeError("Binder Error: table x not found"))
    assert err.kind is ErrorKind.NOT_FOUND
    assert err.to_dict()["repair_owner"] == "model"


def test_permission_error_repair_owner_is_program():
    err = ToolError(ErrorKind.PERMISSION_DENIED, "只读环境")
    assert err.to_dict()["repair_owner"] == "program"
    assert "只读环境" in err.to_prompt()


# ---------------------------------------------------------------- 预算

def test_budget_step_limit():
    budget = Budget(max_steps=2)
    budget.tick_step()
    budget.tick_step()
    with pytest.raises(BudgetExceeded) as exc:
        budget.tick_step()
    assert exc.value.limit == "max_steps"


def test_budget_token_limit():
    budget = Budget(max_tokens=100)
    with pytest.raises(BudgetExceeded):
        budget.add_usage(Usage(total_tokens=150))


def test_budget_tool_limit():
    budget = Budget(max_tool_calls=1)
    budget.reserve_tool()
    with pytest.raises(BudgetExceeded):
        budget.reserve_tool()


# ---------------------------------------------------------------- 上下文

def _msg(role: Role, content: str, **kw) -> Message:
    return Message(role=role, content=content, **kw)


def test_context_truncates_long_tool_output():
    cm = ContextManager(token_budget=100_000, tool_output_chars=200)
    messages = [
        _msg(Role.SYSTEM, "system"),
        _msg(Role.USER, "task"),
        _msg(Role.TOOL, "x" * 5000, tool_call_id="c1"),
    ]
    packed, stats = cm.compact(messages)
    assert stats.truncated_outputs == 1
    assert len(packed[-1].content) < 600
    assert "截断" in packed[-1].content


def test_context_compacts_middle_history():
    cm = ContextManager(token_budget=120, keep_tail=4, tool_output_chars=400)
    messages = [_msg(Role.SYSTEM, "s"), _msg(Role.USER, "u")]
    for i in range(12):
        messages.append(_msg(Role.ASSISTANT, f"分析{i}", tool_calls=[ToolCall(name="run_sql", arguments="{}")]))
        messages.append(_msg(Role.TOOL, "结果" * 80, tool_call_id=f"c{i}"))
    packed, stats = cm.compact(messages)
    assert stats.compacted is True
    assert stats.dropped_messages > 0
    assert packed[0].role is Role.SYSTEM
    assert any(m.name == "harness_summary" for m in packed)


# ---------------------------------------------------------------- 注册表

def test_registry_schema_and_duplicate():
    reg = ToolRegistry()

    class Args(BaseModel):
        sql: str = Field(description="SQL")

    async def handler(args, ctx):  # pragma: no cover
        return {"ok": True}

    spec = ToolSpec(name="run_sql", description="执行 SQL", args_model=Args, handler=handler)
    reg.register(spec)
    schema = reg.openai_tools()[0]
    assert schema["function"]["name"] == "run_sql"
    assert "sql" in schema["function"]["parameters"]["properties"]
    assert "title" not in str(schema)  # 去掉 pydantic 的 title 噪音
    with pytest.raises(ValueError):
        reg.register(spec)


# ---------------------------------------------------------------- 执行器

class EchoArgs(BaseModel):
    value: int


@pytest.fixture
def ctx(settings, tmp_path: Path) -> ToolContext:
    return ToolContext(run_id="run_test", settings=settings, artifacts_dir=tmp_path / "artifacts")


@pytest.fixture
def executor() -> ToolExecutor:
    reg = ToolRegistry()

    async def ok(args: EchoArgs, ctx):
        return {"value": args.value}

    async def boom(args: EchoArgs, ctx):
        raise RuntimeError("Binder Error: table nope does not exist")

    async def slow(args: EchoArgs, ctx):
        await asyncio.sleep(5)

    async def flaky(args: EchoArgs, ctx):
        flaky.calls += 1
        if flaky.calls < 2:
            raise TimeoutError("temporary")
        return {"value": args.value, "retried": True}

    flaky.calls = 0

    reg.register(ToolSpec(name="ok", description="ok", args_model=EchoArgs, handler=ok))
    reg.register(ToolSpec(name="boom", description="boom", args_model=EchoArgs, handler=boom, max_retries=0))
    reg.register(ToolSpec(name="slow", description="slow", args_model=EchoArgs, handler=slow, timeout_s=0.2, max_retries=0))
    reg.register(ToolSpec(name="flaky", description="flaky", args_model=EchoArgs, handler=flaky, max_retries=2))
    return ToolExecutor(reg, default_timeout_s=2)


async def test_executor_ok_and_repair(executor, ctx):
    result = await executor.execute(ToolCall(name="ok", arguments='{"value": "42"}'), ctx)
    assert result.ok
    assert result.data["value"] == 42
    assert result.repairs


async def test_executor_unknown_tool(executor, ctx):
    result = await executor.execute(ToolCall(name="nope", arguments="{}"), ctx)
    assert not result.ok
    assert result.error.kind is ErrorKind.UNKNOWN_TOOL


async def test_executor_invalid_json(executor, ctx):
    result = await executor.execute(ToolCall(name="ok", arguments="{not json"), ctx)
    assert not result.ok
    assert result.error.kind is ErrorKind.INVALID_JSON


async def test_executor_validation_error(executor, ctx):
    result = await executor.execute(ToolCall(name="ok", arguments='{"value": "abc"}'), ctx)
    assert not result.ok
    assert result.error.kind is ErrorKind.INVALID_ARGUMENT
    assert result.error.field_errors


async def test_executor_timeout(executor, ctx):
    result = await executor.execute(ToolCall(name="slow", arguments='{"value": 1}'), ctx)
    assert not result.ok
    assert result.error.kind is ErrorKind.TIMEOUT


async def test_executor_retries_transient_error(executor, ctx):
    result = await executor.execute(ToolCall(name="flaky", arguments='{"value": 7}'), ctx)
    assert result.ok
    assert result.attempts == 2
    assert result.data["retried"] is True


async def test_executor_classifies_not_found(executor, ctx):
    result = await executor.execute(ToolCall(name="boom", arguments='{"value": 1}'), ctx)
    assert not result.ok
    assert result.error.kind is ErrorKind.NOT_FOUND


async def test_executor_emits_artifacts_in_event(tmp_path: Path):
    reg = ToolRegistry()

    class Args(BaseModel):
        code: str

    async def produce(args: Args, context):
        return {"ok": True, "artifacts": ["py_1/trend.png"]}

    reg.register(ToolSpec(name="produce", description="d", args_model=Args, handler=produce))
    events: list[tuple[str, dict]] = []
    context = ToolContext(
        run_id="run_art",
        settings=type("S", (), {"tool_timeout_s": 5})(),
        artifacts_dir=tmp_path,
        emit=lambda name, payload: events.append((name, payload)),
    )
    result = await ToolExecutor(reg).execute(ToolCall(name="produce", arguments='{"code":"x"}'), context)
    assert result.ok
    finished = next(p for n, p in events if n == "tool_finished")
    assert finished["artifacts"] == ["py_1/trend.png"]


# ---------------------------------------------------------------- 状态与追踪

def test_state_store_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.save("run_1", session_id="s1", task="调查", title="调查", status="running", step=2,
               messages=[{"role": "user", "content": "hi"}], usage={"total_tokens": 10})
    row = store.load("run_1")
    assert row["task"] == "调查"
    assert row["messages"][0]["content"] == "hi"
    assert row["usage"]["total_tokens"] == 10

    store.save("run_1", status="completed", answer="done")
    assert store.load("run_1")["status"] == "completed"
    assert len(store.list_runs()) == 1

    store.save("run_2", report={"title": "报告"}, status="completed")
    assert len(store.list_reports()) == 1
    store.close()


def test_state_archive_session_keeps_report_runs(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.save(
        "r1",
        session_id="s1",
        task="三号车间电耗为什么升高",
        status="completed",
        report={"title": "调查报告", "summary": "摘要"},
    )
    store.save("r2", session_id="s1", task="那看一下9月份的", status="completed")
    store.save("r3", session_id="s2", task="别的会话", status="completed")

    result = store.archive_session("s1")
    assert result is not None
    assert [item["run_id"] for item in result["kept"]] == ["r1"]
    assert result["removed"] == ["r2"]

    kept = store.load("r1")
    assert kept["session_id"] is None
    assert kept["task"] == "三号车间电耗为什么升高"
    assert kept["report"]["title"] == "调查报告"
    assert kept["answer"] is None
    assert not kept.get("messages")

    assert store.load("r2") is None, "无报告的 run 应被删除"
    assert store.session_runs("s1") == []
    assert len(store.session_runs("s2")) == 1
    assert store.archive_session("s1") is None
    store.close()


def test_schema_migration_from_legacy_db(tmp_path: Path):
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, session_id TEXT, task TEXT, title TEXT, "
        "status TEXT, step INTEGER, messages TEXT, usage TEXT, answer TEXT, report TEXT, "
        "error TEXT, created_at REAL, updated_at REAL)"
    )
    con.execute("INSERT INTO runs (run_id, task, status) VALUES ('old', '老任务', 'completed')")
    con.commit()
    con.close()

    store = StateStore(db)
    assert store.schema_version == 2
    columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "resume_count" in columns
    assert store.load("old")["task"] == "老任务"
    store.close()


def test_mark_interrupted_runs(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.save("r1", session_id="s1", task="跑一半", status="running")
    store.save("r2", session_id="s1", task="已结束", status="completed")

    assert store.mark_interrupted_runs() == 1
    assert store.load("r1")["status"] == "interrupted"
    assert store.load("r2")["status"] == "completed"
    assert store.mark_interrupted_runs() == 0
    store.close()


def test_session_history_returns_finished_turns(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.save("r1", session_id="s1", task="第一问", status="completed", answer="第一答")
    store.save(
        "r2",
        session_id="s1",
        task="第二问",
        status="completed",
        report={"title": "T", "summary": "第二轮的结论"},
    )
    store.save("r3", session_id="s1", task="进行中", status="running")
    store.save("r4", session_id="s2", task="别的会话", status="completed", answer="x")

    history = store.session_history("s1")
    tasks = [item["task"] for item in history]
    assert tasks == ["第一问", "第二问"]
    assert history[0]["answer"] == "第一答"
    assert history[1]["report"]["summary"] == "第二轮的结论"

    excluded = store.session_history("s1", exclude_run_id="r2")
    assert [item["task"] for item in excluded] == ["第一问"]
    store.close()


def test_tracer_writes_and_reloads(tmp_path: Path):
    tracer = Tracer("run_x", tmp_path)
    tracer.emit(Event(seq=1, type="run_started", run_id="run_x", data={"task": "t"}))
    tracer.emit(Event(seq=2, type="run_finished", run_id="run_x", data={"status": "completed"}))
    events = Tracer.load_events("run_x", tmp_path)
    assert [e["type"] for e in events] == ["run_started", "run_finished"]
    assert tracer.artifacts_dir.exists()
