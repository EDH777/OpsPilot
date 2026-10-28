"""自研 Agent Loop。

一个 run 的生命周期：
    初始化系统提示 + 任务
      → [循环] 压缩上下文 → 调模型 → 有 tool_calls? 执行 → 回填 Observation
      → 模型给出最终回答 或 submit_report 或 预算耗尽 / 错误
      → 落盘状态 + Trace，返回 RunResult

刻意不用 LangGraph / Agents SDK：这一层是本项目的技术主体，也是消融实验的变量。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from opspilot.config import Settings, get_settings
from opspilot.core.events import Event, EventType
from opspilot.core.routing import TaskProfile, classify_task
from opspilot.core.types import (
    Message,
    Role,
    RunStatus,
    StepRecord,
    ToolCall,
    Usage,
    new_id,
)
from opspilot.core.prompts import build_scope_prompt, build_system_prompt
from opspilot.harness.budget import Budget, BudgetExceeded
from opspilot.harness.context import ContextManager
from opspilot.harness.executor import ToolExecutor
from opspilot.harness.registry import ToolContext, ToolRegistry, build_default_registry
from opspilot.harness.state import StateStore
from opspilot.harness.tracing import Tracer
from opspilot.llm.base import BaseLLM, LLMError
from opspilot.security.audit import AuditLog
from opspilot.security.model import Principal

EventCallback = Callable[[Event], None]

BUDGET_NUDGE = (
    "【Harness 预算提醒】本次运行的 token 预算已消耗约 50%。"
    "如果关键结论已有 2~3 条相互印证的证据，请立即调用 submit_report 收尾；"
    "不要再为追求完美重复查询同一指标。若确有未完成项，请写进报告的 limitations。"
)

HISTORY_NOTE = (
    "【会话历史】以下是同一会话中此前的轮次，仅用于理解指代关系（例如“那看一下 9 月份的”），"
    "并沿用用户已经指定的车间、时间口径等约束。不要复述历史内容，也不要重复执行已完成的查询。"
)


@dataclass
class RunResult:
    run_id: str
    session_id: str | None
    status: RunStatus
    task: str
    profile: str = "investigation"
    answer: str | None = None
    report: dict[str, Any] | None = None
    messages: list[Message] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    budget: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    trace_path: str = ""

    @property
    def duration_ms(self) -> float:
        return round((self.finished_at - self.started_at) * 1000, 1) if self.finished_at else 0.0

    def to_dict(self, *, include_messages: bool = False) -> dict[str, Any]:
        data = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "task": self.task,
            "profile": self.profile,
            "answer": self.answer,
            "report": self.report,
            "steps": [s.to_dict() for s in self.steps],
            "usage": self.usage.to_dict(),
            "budget": self.budget,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "trace_path": self.trace_path,
        }
        if include_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
        return data


def sanitize_tool_calls(calls: list[ToolCall]) -> list[ToolCall]:
    """保证回填进历史的 tool_call.arguments 一定是合法 JSON。

    模型被 max_tokens 截断时会产出半截 JSON；原样回传会让下一轮请求直接 400。
    这里替换成占位对象，执行器仍用原始参数报 INVALID_JSON，模型能看见真实错误。
    """
    cleaned: list[ToolCall] = []
    for call in calls:
        try:
            json.loads(call.arguments or "{}")
            cleaned.append(call)
        except json.JSONDecodeError:
            cleaned.append(
                ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=json.dumps(
                        {"_unparsed_arguments": (call.arguments or "")[:800]}, ensure_ascii=False
                    ),
                )
            )
    return cleaned


class AgentLoop:
    def __init__(
        self,
        llm: BaseLLM,
        *,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
        state: StateStore | None = None,
        executor: ToolExecutor | None = None,
        context: ContextManager | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or get_settings()
        self.registry = registry or build_default_registry()
        self.state = state or StateStore(self.settings.state_db_path)
        self.executor = executor or ToolExecutor(self.registry, default_timeout_s=self.settings.tool_timeout_s)
        self.context = context
        self.audit = audit or AuditLog(self.settings.runs_dir)

    # ------------------------------------------------------------------ 分级预算

    def _budget_for(self, profile: TaskProfile) -> Budget:
        s = self.settings
        if profile is TaskProfile.SIMPLE:
            return Budget(
                max_steps=s.simple_max_steps,
                max_tool_calls=s.simple_max_tool_calls,
                max_tokens=s.simple_max_tokens,
                max_seconds=s.simple_max_seconds,
            )
        return Budget(
            max_steps=s.max_steps,
            max_tool_calls=s.max_tool_calls,
            max_tokens=s.max_tokens,
            max_seconds=s.max_seconds,
        )

    def _context_budget(self, profile: TaskProfile) -> int:
        return (
            self.settings.simple_context_token_budget
            if profile is TaskProfile.SIMPLE
            else self.settings.context_token_budget
        )

    # ------------------------------------------------------------------ 对外

    async def run(
        self,
        task: str,
        *,
        principal: Principal,
        session_id: str | None = None,
        run_id: str | None = None,
        emit: EventCallback | None = None,
    ) -> RunResult:
        run_id = run_id or new_id("run_")
        session_id = session_id or new_id("sess_")
        profile = classify_task(task)

        messages = [
            Message(role=Role.SYSTEM, content=build_system_prompt(mode=profile.value)),
            Message(role=Role.SYSTEM, name="harness_scope", content=build_scope_prompt(principal)),
        ]
        history = self._history_messages(session_id, principal)
        if history:
            messages.append(Message(role=Role.SYSTEM, name="harness_history", content=HISTORY_NOTE))
            messages.extend(history)
        messages.append(Message(role=Role.USER, content=task))

        budget = self._budget_for(profile)
        context = self.context or ContextManager(token_budget=self._context_budget(profile))
        return await self._drive(
            run_id=run_id,
            session_id=session_id,
            task=task,
            messages=messages,
            budget=budget,
            usage=Usage(),
            emit=emit,
            context=context,
            profile=profile,
            principal=principal,
        )

    def _history_messages(
        self, session_id: str, principal: Principal, *, max_chars: int = 2400
    ) -> list[Message]:
        """把同一会话此前轮次的「提问 + 结论摘要」拼回上下文，控制总量。"""
        try:
            rows = self.state.session_history(
                session_id, limit=4, owner_user_id=principal.user_id
            )
        except Exception:  # noqa: BLE001 - 历史不可用不应阻断新任务
            return []

        pairs: list[tuple[str, str]] = []
        for row in rows:
            task = (row.get("task") or "").strip()
            if not task:
                continue
            summary = ((row.get("report") or {}).get("summary") or row.get("answer") or "").strip()
            pairs.append((task[:400], summary[:600]))

        selected: list[tuple[str, str]] = []
        used = 0
        for task_text, summary_text in reversed(pairs):
            cost = len(task_text) + len(summary_text)
            if selected and used + cost > max_chars:
                break
            selected.append((task_text, summary_text))
            used += cost

        messages: list[Message] = []
        for task_text, summary_text in reversed(selected):
            messages.append(Message(role=Role.USER, content=task_text))
            messages.append(Message(role=Role.ASSISTANT, content=summary_text or "（本轮已完成，无文字结论）"))
        return messages

    async def resume(
        self, run_id: str, *, principal: Principal, emit: EventCallback | None = None
    ) -> RunResult:
        row = self.state.load(run_id)
        if not row or row.get("owner_user_id") != principal.user_id:
            raise ValueError(f"找不到运行记录 {run_id}")
        if row.get("status") not in {
            RunStatus.RUNNING.value,
            RunStatus.PAUSED.value,
            RunStatus.FAILED.value,
            RunStatus.INTERRUPTED.value,
        }:
            raise ValueError(f"运行 {run_id} 状态为 {row.get('status')}，不可续跑")

        self.state.bump_resume(run_id)

        messages = [Message.from_dict(m) for m in (row.get("messages") or [])]
        usage_data = row.get("usage") or {}
        task = row.get("task") or ""
        profile = classify_task(task)
        budget = self._budget_for(profile)
        budget.steps = int(row.get("step") or 0)
        budget.tokens = int(usage_data.get("total_tokens") or 0)
        context = self.context or ContextManager(token_budget=self._context_budget(profile))
        return await self._drive(
            run_id=run_id,
            session_id=row.get("session_id") or new_id("sess_"),
            task=task,
            messages=messages,
            budget=budget,
            usage=Usage(**usage_data) if usage_data else Usage(),
            emit=emit,
            context=context,
            profile=profile,
            resumed=True,
            principal=principal,
        )

    # ------------------------------------------------------------------ 主循环

    async def _drive(
        self,
        *,
        run_id: str,
        session_id: str,
        task: str,
        messages: list[Message],
        budget: Budget,
        usage: Usage,
        emit: EventCallback | None,
        context: ContextManager,
        profile: TaskProfile,
        principal: Principal,
        resumed: bool = False,
    ) -> RunResult:
        tracer = Tracer(run_id, self.settings.runs_dir)
        start_seq = len(Tracer.load_events(run_id, self.settings.runs_dir))
        seq = start_seq
        started_at = time.time()

        def publish(etype: EventType | str, **data: Any) -> None:
            nonlocal seq
            seq += 1
            event = Event(
                seq=seq,
                type=etype.value if isinstance(etype, EventType) else etype,
                run_id=run_id,
                data=data,
            )
            tracer.emit(event)
            if emit:
                try:
                    emit(event)
                except Exception:  # noqa: BLE001 - 订阅方故障不能影响主流程
                    pass

        def tool_publish(name: str, payload: dict[str, Any]) -> None:
            publish(name, **payload)

        ctx = ToolContext(
            run_id=run_id,
            principal=principal,
            audit=self.audit,
            settings=self.settings,
            artifacts_dir=tracer.artifacts_dir,
            emit=tool_publish,
        )

        status = RunStatus.RUNNING
        answer: str | None = None
        error: str | None = None
        report: dict[str, Any] | None = None
        steps: list[StepRecord] = []
        failure_streak: dict[str, int] = {}
        nudged = False

        self.state.save(
            run_id,
            session_id=session_id,
            task=task,
            title=task[:48],
            status=RunStatus.RUNNING.value,
            messages=[m.to_dict() for m in messages],
            usage=usage.to_dict(),
            step=budget.steps,
            owner_user_id=principal.user_id,
            owner_role=principal.role,
        )
        self.audit.record(
            action="run.start",
            decision="allow",
            user_id=principal.user_id,
            role=principal.role,
            run_id=run_id,
            detail={"scope": principal.as_scope(), "profile": profile.value},
        )
        publish(
            EventType.RUN_STARTED,
            task=task,
            session_id=session_id,
            resumed=resumed,
            model=self.llm.model_name,
            tools=self.registry.names_for(principal),
            budget=budget.snapshot(),
            profile=profile.value,
            profile_label=profile.label,
            owner_user_id=principal.user_id,
            owner_role=principal.role,
            scope=principal.as_scope(),
        )

        try:
            while status is RunStatus.RUNNING:
                try:
                    budget.tick_step()
                except BudgetExceeded as exc:
                    status = RunStatus.BUDGET_EXCEEDED
                    error = exc.message
                    publish(EventType.BUDGET_EXCEEDED, limit=exc.limit, message=exc.message, budget=budget.snapshot())
                    break

                step_started = time.time()
                step = StepRecord(index=budget.steps, started_at=step_started)

                packed, stats = context.compact(messages)
                if stats.compacted or stats.truncated_outputs:
                    publish(EventType.CONTEXT_COMPACTED, step=budget.steps, **stats.to_dict())

                publish(
                    EventType.LLM_REQUEST,
                    step=budget.steps,
                    messages=len(packed),
                    est_tokens=stats.final_tokens,
                )
                try:
                    response = await self.llm.complete(packed, self.registry.openai_tools())
                except LLMError as exc:
                    status = RunStatus.FAILED
                    error = str(exc)
                    publish(EventType.ERROR, stage="llm", message=str(exc), status=exc.status)
                    break

                usage = usage.add(response.usage)
                step.usage = response.usage
                try:
                    budget.add_usage(response.usage)
                except BudgetExceeded as exc:
                    status = RunStatus.BUDGET_EXCEEDED
                    error = exc.message
                    publish(EventType.BUDGET_EXCEEDED, limit=exc.limit, message=exc.message, budget=budget.snapshot())
                    break

                assistant = Message(
                    role=Role.ASSISTANT,
                    content=response.content,
                    # 历史里必须放合法 JSON：模型可能因 max_tokens 截断产出半截参数，
                    # 直接回传会让下一轮请求被网关 400 拒绝。执行仍用原始参数，
                    # 由执行器给出 INVALID_JSON 的结构化 Observation。
                    tool_calls=sanitize_tool_calls(response.tool_calls),
                    reasoning=response.reasoning,
                )
                messages.append(assistant)
                step.assistant_text = response.content
                step.tool_names = [c.name for c in response.tool_calls]
                step.stop_reason = response.finish_reason

                publish(
                    EventType.LLM_RESPONSE,
                    step=budget.steps,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=[
                        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                    finish_reason=response.finish_reason,
                    usage=response.usage.to_dict(),
                    latency_ms=response.latency_ms,
                )

                if response.content:
                    publish(EventType.ASSISTANT_MESSAGE, step=budget.steps, content=response.content)

                if response.finish_reason == "length":
                    publish(
                        "llm_truncated",
                        step=budget.steps,
                        message="模型输出达到 max_tokens 被截断，工具参数可能不完整；下一次尽量精简输出。",
                    )

                if not response.tool_calls:
                    answer = response.content
                    status = RunStatus.COMPLETED
                    step.finished_at = time.time()
                    steps.append(step)
                    break

                report_received = False
                for call in response.tool_calls:
                    try:
                        budget.reserve_tool()
                    except BudgetExceeded as exc:
                        status = RunStatus.BUDGET_EXCEEDED
                        error = exc.message
                        publish(EventType.BUDGET_EXCEEDED, limit=exc.limit, message=exc.message, budget=budget.snapshot())
                        break

                    result = await self.executor.execute(call, ctx)
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            content=result.content_for_llm(),
                            tool_call_id=call.id,
                            name=call.name,
                            meta={
                                "ok": result.ok,
                                "latency_ms": result.latency_ms,
                                "attempts": result.attempts,
                                "repairs": result.repairs,
                                "error_kind": result.error.kind.value if result.error else None,
                            },
                        )
                    )

                    if result.ok:
                        step.tool_ok += 1
                        failure_streak.pop(f"{call.name}:{call.arguments}", None)
                    else:
                        step.tool_failed += 1
                        key = f"{call.name}:{call.arguments}"
                        failure_streak[key] = failure_streak.get(key, 0) + 1
                        if failure_streak[key] >= 2:
                            messages.append(
                                Message(
                                    role=Role.SYSTEM,
                                    name="harness_guard",
                                    content=(
                                        "【Harness 熔断提示】同一工具调用已连续失败两次。"
                                        "不要再原样重试：请换参数、换工具或换调查路径；若确实无法完成，"
                                        "请在报告中说明受限原因。"
                                    ),
                                )
                            )
                            publish("loop_guard", name=call.name, reason="repeated_failure")

                    if result.ok and call.name == "submit_report" and ctx.shared.get("report"):
                        report = ctx.shared["report"]
                        report_received = True

                step.finished_at = time.time()
                steps.append(step)

                if report_received:
                    answer = answer or (report or {}).get("summary")
                    status = RunStatus.COMPLETED
                    publish(EventType.REPORT, report=report)
                    break

                # 预算感知：消耗过半时提醒模型收尾，避免"一直查下去"烧穿预算
                if not nudged and (
                    usage.total_tokens > self.settings.max_tokens * 0.5
                    or budget.steps >= self.settings.max_steps * 0.7
                ):
                    nudged = True
                    messages.append(Message(role=Role.SYSTEM, name="harness_budget", content=BUDGET_NUDGE))
                    publish(
                        "budget_warning",
                        message="预算已消耗约 50%，已提醒模型尽快提交报告",
                        tokens=usage.total_tokens,
                        max_tokens=self.settings.max_tokens,
                    )

                # 每步之后落检查点：崩溃/重启后可从最后一步续跑
                self.state.save(
                    run_id,
                    status=RunStatus.RUNNING.value,
                    messages=[m.to_dict() for m in messages],
                    usage=usage.to_dict(),
                    step=budget.steps,
                )
                publish(EventType.CHECKPOINT, step=budget.steps, tokens=usage.total_tokens)

        except asyncio.CancelledError:
            status = RunStatus.CANCELLED
            error = "运行被取消"
            publish(EventType.ERROR, stage="loop", message=error)
        except Exception as exc:  # noqa: BLE001 - 兜底：记录堆栈后以 FAILED 结束，不让服务崩溃
            import traceback

            status = RunStatus.FAILED
            error = f"{type(exc).__name__}: {exc}"
            publish(
                EventType.ERROR,
                stage="loop",
                message=error,
                traceback=traceback.format_exc()[-4000:],
            )

        finished_at = time.time()
        if status is RunStatus.COMPLETED and not answer and report:
            answer = report.get("summary")
        if not answer:
            # 预算耗尽/异常终止时，保留模型最后一段阶段性结论，避免前端一片空白
            for msg in reversed(messages):
                if msg.role is Role.ASSISTANT and msg.content:
                    answer = msg.content
                    break

        self.state.save(
            run_id,
            status=status.value,
            messages=[m.to_dict() for m in messages],
            usage=usage.to_dict(),
            step=budget.steps,
            answer=answer,
            report=report,
            error=error,
        )
        publish(
            EventType.RUN_FINISHED,
            status=status.value,
            answer=answer,
            error=error,
            usage=usage.to_dict(),
            budget=budget.snapshot(),
            steps=len(steps),
            duration_ms=round((finished_at - started_at) * 1000, 1),
        )

        return RunResult(
            run_id=run_id,
            session_id=session_id,
            status=status,
            task=task,
            profile=profile.value,
            answer=answer,
            report=report,
            messages=messages,
            steps=steps,
            usage=usage,
            budget=budget.snapshot(),
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            trace_path=str(tracer.trace_path),
        )
