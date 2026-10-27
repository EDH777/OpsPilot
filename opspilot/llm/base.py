"""LLM 抽象层。Harness 只依赖这个接口，便于替换供应商 / 注入 Mock 做确定性测试。"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from opspilot.core.types import Message, ToolCall, Usage


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    reasoning: str | None = None
    finish_reason: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class BaseLLM(abc.ABC):
    """所有模型客户端的最小契约：一次补全，返回文本和/或工具调用。"""

    model_name: str = "unknown"

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - 默认无资源
        return None
