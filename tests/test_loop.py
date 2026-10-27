"""Agent Loop 端到端测试：完整调查、失败熔断、预算终止、状态续跑。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opspilot.core.loop import AgentLoop
from opspilot.core.types import Message, Role, RunStatus, ToolCall
from opspilot.harness.state import StateStore
from opspilot.llm.mock import ScriptedLLM, assistant, tool_call

SQL = "SELECT workshop_id FROM workshop ORDER BY 1"
REPORT = {
    "title": "测试调查报告",
    "summary": "三号车间单位产品电耗上升 13.2%，证据指向空压机长期低负载空转。",
    "metrics": [{"name": "单位产品电耗", "value": 3.19, "unit": "kWh/件", "period": "2026-09-28~10-20"}],
    "findings": [
        {"statement": "单位电耗上升", "evidence": "SQL 对比结果", "confidence": "已验证事实"},
        {"statement": "空压机空转", "evidence": "equipment_operation 空转时长", "confidence": "证据支持"},
    ],
    "recommendations": ["加装变频"],
    "limitations": ["未核实维修记录"],
}


def _loop(script, settings, store, registry):
    return AgentLoop(ScriptedLLM(script), settings=settings, state=store, registry=registry)


async def test_full_investigation_completes_with_report(settings, state_store, registry):
    events: list[dict] = []
    script = [
        assistant(tool_calls=[tool_call("inspect_schema", {})]),
        assistant(tool_calls=[tool_call("run_sql", {"sql": SQL, "purpose": "看车间"})]),
        assistant(tool_calls=[tool_call("submit_report", REPORT)]),
    ]
    loop = _loop(script, settings, state_store, registry)
    result = await loop.run("测试调查", emit=lambda e: events.append(e.to_dict()))

    assert result.status is RunStatus.COMPLETED
    assert result.report["title"] == "测试调查报告"
    assert result.budget["steps"] == 3
    assert result.budget["tool_calls"] == 3
    assert Path(result.trace_path).exists()

    types = [e["type"] for e in events]
    assert "run_started" in types
    assert "tool_started" in types
    assert "tool_finished" in types
    assert "report" in types
    assert "run_finished" in types

    row = state_store.load(result.run_id)
    assert row["status"] == "completed"
    assert row["report"]["title"] == "测试调查报告"
    assert row["messages"]


async def test_tool_error_is_fed_back_and_model_recovers(settings, state_store, registry):
    calls: list[list[Message]] = []

    def capture(messages, tools):
        calls.append(messages)
        if len(calls) == 1:
            return assistant(tool_calls=[tool_call("run_sql", {"sql": "SELECT * FROM not_exist"})])
        if len(calls) == 2:
            return assistant(tool_calls=[tool_call("run_sql", {"sql": SQL})])
        return assistant(content="调查完成，结论见报告。")

    loop = _loop([capture], settings, state_store, registry)
    result = await loop.run("错误恢复测试")

    assert result.status is RunStatus.COMPLETED
    tool_messages = [m for m in result.messages if m.role is Role.TOOL]
    assert json.loads(tool_messages[0].content)["ok"] is False
    assert json.loads(tool_messages[0].content)["error"]["kind"] in {"not_found", "invalid_argument"}


async def test_repeated_failure_triggers_guard_message(settings, state_store, registry):
    events: list[dict] = []
    bad_args = {"sql": "SELECT * FROM missing_table"}
    script = [
        assistant(tool_calls=[tool_call("run_sql", bad_args, call_id="c1")]),
        assistant(tool_calls=[tool_call("run_sql", bad_args, call_id="c2")]),
        assistant(content="无法完成，表不存在。"),
    ]
    loop = _loop(script, settings, state_store, registry)
    result = await loop.run("重复失败测试", emit=lambda e: events.append(e.to_dict()))

    guards = [m for m in result.messages if m.name == "harness_guard"]
    assert guards, "应注入熔断提示，阻止模型原样重试"
    assert any(e["type"] == "loop_guard" for e in events)


async def test_budget_exceeded_stops_loop(settings, state_store, registry):
    tight = settings.model_copy(update={"max_steps": 1})
    script = [
        assistant(tool_calls=[tool_call("inspect_schema", {})]),
        assistant(content="还想继续查"),
    ]
    loop = _loop(script, tight, state_store, registry)
    result = await loop.run("预算测试")

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.budget["steps"] == 1
    assert state_store.load(result.run_id)["status"] == "budget_exceeded"


async def test_resume_from_checkpoint(settings, tmp_path, registry):
    store = StateStore(tmp_path / "resume.sqlite3")
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="继续调查"),
        Message(role=Role.ASSISTANT, content=None, tool_calls=[ToolCall(id="c1", name="run_sql", arguments='{"sql": "SELECT 1"}')]),
        Message(role=Role.TOOL, content='{"ok": true}', tool_call_id="c1", name="run_sql"),
    ]
    store.save(
        "run_resume",
        session_id="s1",
        task="继续调查",
        status="running",
        step=1,
        messages=[m.to_dict() for m in messages],
        usage={"total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40, "llm_calls": 1},
    )

    loop = _loop([assistant(content="已完成续跑。")], settings, store, registry)
    result = await loop.resume("run_resume")

    assert result.status is RunStatus.COMPLETED
    assert result.budget["steps"] == 2  # 从已保存的第 1 步继续
    assert store.load("run_resume")["answer"] == "已完成续跑。"
    store.close()


async def test_resume_rejects_finished_run(settings, state_store, registry):
    state_store.save("run_done", status="completed", task="t", messages=[])
    loop = _loop([], settings, state_store, registry)
    with pytest.raises(ValueError):
        await loop.resume("run_done")


async def test_second_run_inherits_session_history(settings, state_store, registry):
    captured: list[list[Message]] = []

    def capture(messages, tools):
        captured.append(list(messages))
        return assistant(content="收到")

    loop = AgentLoop(
        ScriptedLLM([capture, capture]), settings=settings, state=state_store, registry=registry
    )
    await loop.run("看一下二号车间这个月的用电情况", session_id="sess_ctx")
    await loop.run("那看一下9月份的", session_id="sess_ctx")

    second = captured[1]
    texts = [m.content or "" for m in second]
    assert any("看一下二号车间这个月的用电情况" in t for t in texts), "第二轮应带上第一轮提问"
    assert any(t == "收到" for t in texts), "第二轮应带上第一轮结论"
    assert any(m.name == "harness_history" for m in second), "应插入会话历史说明"


async def test_resume_interrupted_run_increments_count(settings, tmp_path, registry):
    store = StateStore(tmp_path / "interrupted.sqlite3")
    messages = [Message(role=Role.SYSTEM, content="s"), Message(role=Role.USER, content="继续调查")]
    store.save(
        "run_int",
        session_id="s1",
        task="继续调查",
        status="interrupted",
        step=1,
        messages=[m.to_dict() for m in messages],
        usage={"total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40, "llm_calls": 1},
    )
    loop = AgentLoop(
        ScriptedLLM([assistant(content="已继续完成")]), settings=settings, state=store, registry=registry
    )
    result = await loop.resume("run_int")

    assert result.status is RunStatus.COMPLETED
    row = store.load("run_int")
    assert row["status"] == "completed"
    assert row["resume_count"] == 1
    store.close()
