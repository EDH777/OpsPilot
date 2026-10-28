"""统一工具执行器。

职责边界（Harness 的核心价值所在）：
1. 解析模型给出的参数（可能是非法 JSON / 错误类型 / 脏日期格式）；
2. 做**确定性修复**（不进模型，不烧 token）；
3. 用 Pydantic 严格校验，生成结构化字段错误；
4. 加超时、限流退避、可重试错误的重试；
5. 把成功/失败都变成模型可读的 Observation，并回写事件流。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import types
from dataclasses import dataclass, field
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from opspilot.core.types import ToolCall
from opspilot.harness.errors import ErrorKind, ToolError, classify_exception
from opspilot.harness.registry import ToolContext, ToolRegistry, ToolSpec
from opspilot.security.access import bind_scope
from opspilot.security.model import ROLES

_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?$")
_MONTH_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+(\.\d+)?$")
_TRUE = {"true", "1", "yes", "y", "是"}
_FALSE = {"false", "0", "no", "n", "否"}


@dataclass
class ToolResult:
    call_id: str
    name: str
    ok: bool
    data: Any = None
    error: ToolError | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    repairs: list[str] = field(default_factory=list)

    def content_for_llm(self, max_chars: int = 8000) -> str:
        if self.ok:
            payload: dict[str, Any] = {"ok": True, "result": self.data}
            if self.repairs:
                payload["harness_note"] = "已自动修复参数：" + "；".join(self.repairs)
        else:
            payload = {"ok": False, "error": self.error.to_dict() if self.error else {"kind": "internal"}}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            text = text[:max_chars] + f"…（已截断，原始长度 {len(text)} 字符；如需完整数据请缩小查询范围）"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "ok": self.ok,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "repairs": self.repairs,
        }


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, default_timeout_s: float = 60.0) -> None:
        self.registry = registry
        self.default_timeout_s = default_timeout_s

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        repairs: list[str] = []
        attempts = 0
        ctx.publish(
            "tool_started",
            call_id=call.id,
            name=call.name,
            arguments=call.arguments[:2000],
        )

        try:
            # 先判“存在但未授权”与“根本不存在”：
            # 已注册但角色未授权 -> PERMISSION_DENIED；未注册 -> UNKNOWN_TOOL。
            if self.registry.has(call.name) and call.name not in ROLES[ctx.principal.role].tools:
                return self._deny(
                    call, ctx, started,
                    reason="TOOL_NOT_GRANTED",
                    message="当前角色不能使用该工具",
                    hint="请使用已提供的工具完成分析。",
                )

            spec = self._resolve(call.name, ctx.principal)
            ctx.audit.record(
                action=f"tool.call:{call.name}",
                decision="allow",
                user_id=ctx.principal.user_id,
                role=ctx.principal.role,
                run_id=ctx.run_id,
            )
            raw = self._parse_arguments(call)
            fixed, repairs = repair_arguments(raw, spec.args_model)
            fixed = bind_scope(call.name, fixed, ctx.principal)
            if repairs:
                ctx.publish("arg_repaired", call_id=call.id, name=call.name, repairs=repairs)
            args = self._validate(spec, fixed)

            data, attempts = await self._invoke(spec, args, ctx)
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                ok=True,
                data=data,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                attempts=attempts,
                repairs=repairs,
            )
        except ToolError as exc:
            if exc.kind is ErrorKind.PERMISSION_DENIED:
                ctx.audit.record(
                    action=f"tool.call:{call.name}",
                    decision="deny",
                    user_id=ctx.principal.user_id,
                    role=ctx.principal.role,
                    run_id=ctx.run_id,
                    reason=(exc.message or "PERMISSION_DENIED")[:200],
                )
                ctx.publish(
                    "policy_denied",
                    call_id=call.id,
                    name=call.name,
                    reason="POLICY_DENIED",
                    message=exc.message,
                )
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=exc,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                attempts=max(attempts, 1),
                repairs=repairs,
            )

        ctx.publish(
            "tool_finished",
            call_id=call.id,
            name=call.name,
            ok=result.ok,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
            repairs=result.repairs,
            error=result.error.to_dict() if result.error else None,
            preview=result.content_for_llm(600),
            # 产物清单单独带出来，前端据此渲染图表（不发完整结果，避免事件流过大）
            artifacts=result.data.get("artifacts") if isinstance(result.data, dict) else None,
        )
        return result

    # ------------------------------------------------------------------ 内部

    def _deny(
        self,
        call: ToolCall,
        ctx: ToolContext,
        started: float,
        *,
        reason: str,
        message: str,
        hint: str,
    ) -> ToolResult:
        ctx.audit.record(
            action=f"tool.call:{call.name}",
            decision="deny",
            user_id=ctx.principal.user_id,
            role=ctx.principal.role,
            run_id=ctx.run_id,
            reason=reason,
        )
        ctx.publish(
            "policy_denied",
            call_id=call.id,
            name=call.name,
            reason=reason,
            message=message,
        )
        result = ToolResult(
            call_id=call.id,
            name=call.name,
            ok=False,
            error=ToolError(ErrorKind.PERMISSION_DENIED, message, hint=hint),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            attempts=0,
        )
        # 拒绝也要发结束事件，前端工具气泡才不会一直转圈
        ctx.publish(
            "tool_finished",
            call_id=call.id,
            name=call.name,
            ok=False,
            latency_ms=result.latency_ms,
            attempts=0,
            repairs=[],
            error=result.error.to_dict() if result.error else None,
            preview=result.content_for_llm(600),
            artifacts=None,
        )
        return result

    def _resolve(self, name: str, principal) -> ToolSpec:
        try:
            return self.registry.get(name)
        except KeyError:
            available = "、".join(self.registry.names_for(principal))
            raise ToolError(
                ErrorKind.UNKNOWN_TOOL,
                f"不存在名为 {name} 的工具",
                hint=f"可用工具：{available}。请从这些工具中选择。",
            ) from None

    @staticmethod
    def _parse_arguments(call: ToolCall) -> dict[str, Any]:
        try:
            return call.parsed()
        except ValueError as exc:
            raise ToolError(
                ErrorKind.INVALID_JSON,
                str(exc),
                hint='请输出严格合法的 JSON 对象，例如 {"sql": "SELECT 1"}',
            ) from exc

    @staticmethod
    def _validate(spec: ToolSpec, args: dict[str, Any]) -> BaseModel:
        try:
            return spec.args_model.model_validate(args)
        except ValidationError as exc:
            fields = [
                {
                    "field": ".".join(str(p) for p in err["loc"]) or "(root)",
                    "message": err["msg"],
                    "type": err["type"],
                    "input": _safe(err.get("input")),
                }
                for err in exc.errors()
            ]
            raise ToolError(
                ErrorKind.INVALID_ARGUMENT,
                f"工具 {spec.name} 参数校验未通过",
                hint="请按参数 schema 修正字段名与类型；缺失字段必须补齐。",
                field_errors=fields,
            ) from exc

    async def _invoke(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> tuple[Any, int]:
        timeout = spec.timeout_s or self.default_timeout_s
        max_attempts = max(1, spec.max_retries + 1)
        last: ToolError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await asyncio.wait_for(spec.handler(args, ctx), timeout=timeout), attempt
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一归类，绝不吞掉原因
                err = classify_exception(exc)
                err.details = err.details or _safe(str(exc))[:500]
                last = err
                if not err.retryable or attempt >= max_attempts:
                    raise err from exc
                backoff = 0.4 * (2 ** (attempt - 1))
                ctx.publish(
                    "tool_retry",
                    name=spec.name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    kind=err.kind.value,
                    backoff_s=backoff,
                )
                await asyncio.sleep(backoff)

        raise last or ToolError(ErrorKind.INTERNAL, "工具执行失败")


# --------------------------------------------------------------------- 参数修复

def _unwrap(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_list(annotation: Any) -> bool:
    return get_origin(_unwrap(annotation)) in (list, tuple, set)


def _coerce(value: Any, annotation: Any) -> Any:
    base = _unwrap(annotation)

    if _is_list(base) and isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,，;；]", value) if p.strip()]
        return parts
    if base is int and isinstance(value, bool):
        return int(value)
    if base is int and isinstance(value, str) and _INT_RE.match(value.strip()):
        return int(value.strip())
    if base is float and isinstance(value, str) and _FLOAT_RE.match(value.strip()):
        return float(value.strip())
    if base is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if base is bool and isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    if base is str and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if base is dict and isinstance(value, str):
        try:
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            return value
    return value


def _normalize_temporal(field_name: str, value: str) -> str | None:
    text = value.strip()
    m = _DATE_RE.match(text)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    if "month" in field_name or field_name in {"period"}:
        m = _MONTH_RE.match(text)
        if m:
            y, mo = m.groups()
            return f"{int(y):04d}-{int(mo):02d}"
    return None


def repair_arguments(raw: dict[str, Any], model: type[BaseModel]) -> tuple[dict[str, Any], list[str]]:
    """确定性参数修复：类型转换、列表拆分、日期规范化、剔除未知字段。

    这一步刻意不交给 LLM：格式问题程序能修就程序修，省 token 也省一次往返。
    """
    fields = model.model_fields
    fixed: dict[str, Any] = {}
    notes: list[str] = []

    for key, value in raw.items():
        if key not in fields:
            notes.append(f"忽略未知参数 `{key}`")
            continue

        annotation = fields[key].annotation
        new_value = _coerce(value, annotation)
        if new_value != value or type(new_value) is not type(value):
            notes.append(f"`{key}`：{value!r} → {new_value!r}")

        if isinstance(new_value, str):
            normalized = _normalize_temporal(key, new_value)
            if normalized and normalized != new_value:
                notes.append(f"`{key}`：日期格式规范化 {new_value!r} → {normalized!r}")
                new_value = normalized

        fixed[key] = new_value

    return fixed, notes


def _safe(value: Any, limit: int = 300) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]
