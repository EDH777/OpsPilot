"""工具错误分类与修复提示。

设计原则（对应方案文档）：参数校验和已知错误优先由程序确定性修复；
只有需要语义判断的错误才交还给 LLM；同一失败操作不允许无限重试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_JSON = "invalid_json"
    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UPSTREAM_ERROR = "upstream_error"
    EXECUTION_ERROR = "execution_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERNAL = "internal"

    @property
    def repair_owner(self) -> str:
        """谁来修：program=程序确定性修复；model=需要模型改参数/改路线。"""
        return {
            ErrorKind.INVALID_JSON: "model",
            ErrorKind.INVALID_ARGUMENT: "model",
            ErrorKind.UNKNOWN_TOOL: "model",
            ErrorKind.NOT_FOUND: "model",
            ErrorKind.PERMISSION_DENIED: "program",
            ErrorKind.BUDGET_EXCEEDED: "program",
            ErrorKind.INTERNAL: "program",
        }.get(self, "program")


RETRYABLE_KINDS = {ErrorKind.TIMEOUT, ErrorKind.RATE_LIMIT, ErrorKind.UPSTREAM_ERROR}


@dataclass
class ToolError(Exception):
    kind: ErrorKind
    message: str
    hint: str | None = None
    details: Any = None
    field_errors: list[dict[str, Any]] = field(default_factory=list)
    retryable: bool | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.retryable is None:
            self.retryable = self.kind in RETRYABLE_KINDS

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
            "repair_owner": self.kind.repair_owner,
            "retryable": bool(self.retryable),
        }
        if self.hint:
            data["hint"] = self.hint
        if self.field_errors:
            data["field_errors"] = self.field_errors
        if include_details and self.details is not None:
            data["details"] = self.details
        return data

    def to_prompt(self) -> str:
        """给模型看的紧凑错误描述：说清错在哪、怎么改，但不泄露内部堆栈。"""
        parts = [f"[{self.kind.value}] {self.message}"]
        if self.field_errors:
            for fe in self.field_errors[:6]:
                parts.append(f"- 字段 {fe.get('field')}: {fe.get('message')}")
        if self.hint:
            parts.append(f"建议: {self.hint}")
        return "\n".join(parts)


def classify_exception(exc: BaseException) -> ToolError:
    """把任意异常映射成有语义的错误类型。"""
    import asyncio
    import json

    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ToolError(ErrorKind.TIMEOUT, "工具执行超时", hint="缩小查询范围或减少数据量后重试")
    if isinstance(exc, json.JSONDecodeError):
        return ToolError(ErrorKind.INVALID_JSON, f"参数 JSON 解析失败：{exc.msg}", hint="请输出合法 JSON 对象")
    if isinstance(exc, PermissionError):
        return ToolError(ErrorKind.PERMISSION_DENIED, str(exc) or "权限不足", hint="该操作被安全策略拒绝，请改用只读查询")
    if isinstance(exc, (KeyError, IndexError)):
        return ToolError(ErrorKind.NOT_FOUND, f"找不到目标对象：{exc}", hint="先查询元信息确认名称/ID 是否存在")
    if isinstance(exc, ValueError):
        return ToolError(ErrorKind.INVALID_ARGUMENT, str(exc))

    # 第三方库错误按消息特征归类，避免为每个库引入依赖
    name = type(exc).__name__
    text = str(exc)
    lower = text.lower()
    if name.startswith("HTTPStatusError") or "http" in name.lower():
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return ToolError(ErrorKind.RATE_LIMIT, "上游接口限流", details=text[:400])
        if status is not None and 400 <= status < 500:
            return ToolError(ErrorKind.INVALID_ARGUMENT, f"上游接口拒绝请求（HTTP {status}）", details=text[:400])
        return ToolError(ErrorKind.UPSTREAM_ERROR, f"上游接口异常（HTTP {status}）", details=text[:400])
    if "no such table" in lower or "does not exist" in lower or "not found" in lower:
        return ToolError(ErrorKind.NOT_FOUND, text[:300], hint="先调用 inspect_schema 查看可用的表/字段")
    if "binder error" in lower or "catalog error" in lower or "syntax error" in lower:
        return ToolError(ErrorKind.INVALID_ARGUMENT, text[:400], hint="检查表名、字段名和 SQL 语法后重试")
    if "permission" in lower or "read-only" in lower or "access denied" in lower:
        return ToolError(ErrorKind.PERMISSION_DENIED, text[:300])
    return ToolError(ErrorKind.EXECUTION_ERROR, text[:400] or f"{name} 执行失败", details=text[:1000])
