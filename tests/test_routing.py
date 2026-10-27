"""任务分级测试：路由规则、分档预算、简单任务确实被更早截停。"""

from __future__ import annotations

import pytest

from opspilot.core.loop import AgentLoop
from opspilot.core.routing import TaskProfile, classify_task
from opspilot.core.types import RunStatus
from opspilot.harness.state import StateStore
from opspilot.llm.mock import ScriptedLLM, assistant, tool_call


@pytest.mark.parametrize(
    "task,expected",
    [
        ("看一下二号车间这个月的用电情况", TaskProfile.SIMPLE),
        ("查询 9 月各车间产量", TaskProfile.SIMPLE),
        ("帮我看一下设备告警有多少条", TaskProfile.SIMPLE),
        ("三号车间近30天单位产品电耗为什么升高？", TaskProfile.INVESTIGATION),
        ("分析一下五号车间不良率上升的原因", TaskProfile.INVESTIGATION),
        ("三号车间电耗异常是否与设备有关", TaskProfile.INVESTIGATION),
        ("帮个忙", TaskProfile.INVESTIGATION),  # 拿不准时按调查处理
    ],
)
def test_classify_task(task, expected):
    assert classify_task(task) is expected


def test_profile_budgets(settings, registry, tmp_path):
    loop = AgentLoop(
        ScriptedLLM([]),
        settings=settings,
        registry=registry,
        state=StateStore(tmp_path / "budget.sqlite3"),
    )
    simple = loop._budget_for(TaskProfile.SIMPLE)
    investigation = loop._budget_for(TaskProfile.INVESTIGATION)
    assert simple.max_steps == settings.simple_max_steps < investigation.max_steps
    assert simple.max_tokens == settings.simple_max_tokens < investigation.max_tokens
    assert loop._context_budget(TaskProfile.SIMPLE) < loop._context_budget(TaskProfile.INVESTIGATION)


async def test_simple_task_is_stopped_by_simple_budget(settings, state_store, registry):
    tight = settings.model_copy(update={"simple_max_steps": 2})
    script = [assistant(tool_calls=[tool_call("inspect_schema", {})]) for _ in range(6)]
    loop = AgentLoop(ScriptedLLM(script), settings=tight, state=state_store, registry=registry)

    result = await loop.run("看一下二号车间这个月的用电情况")

    assert result.profile == "simple"
    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.budget["steps"] == 2
    assert result.budget["max_steps"] == 2


async def test_investigation_task_gets_full_budget(settings, state_store, registry):
    loop = AgentLoop(
        ScriptedLLM([assistant(content="直接回答")]),
        settings=settings,
        state=state_store,
        registry=registry,
    )
    result = await loop.run("三号车间电耗为什么升高？")
    assert result.profile == "investigation"
    assert result.budget["max_steps"] == settings.max_steps
