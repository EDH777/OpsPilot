"""核心数据契约：消息、工具调用、用量、运行状态。

这一层刻意不依赖任何框架，保证 Harness 与 LLM 供应商解耦。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def estimate_tokens(text: str | None) -> int:
    """粗略 token 估算，不引入 tiktoken 依赖。

    规则：CJK 字符约 1 token/字；其余按 4 字符 1 token；再叠加 1.15 的安全系数。
    误差在 ±20% 内，只用于预算控制，不用于计费。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len(text) - cjk
    return int((cjk + rest / 4) * 1.15) + 1


@dataclass
class ToolCall:
    """模型发起的一次工具调用。arguments 保留原始字符串，因为模型可能给出非法 JSON。"""

    name: str
    arguments: str = "{}"
    id: str = field(default_factory=lambda: new_id("call_"))

    def parsed(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"工具参数不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        return value

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role.value}
        if self.role is Role.ASSISTANT and self.tool_calls:
            data["content"] = self.content or ""
            data["tool_calls"] = [c.to_openai() for c in self.tool_calls]
            return data
        if self.role is Role.TOOL:
            data["content"] = self.content or ""
            data["tool_call_id"] = self.tool_call_id
            return data
        data["content"] = self.content or ""
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_openai(cls, raw: dict[str, Any]) -> Message:
        calls = [
            ToolCall(
                id=c.get("id") or new_id("call_"),
                name=c["function"]["name"],
                arguments=c["function"].get("arguments") or "{}",
            )
            for c in (raw.get("tool_calls") or [])
        ]
        return cls(
            role=Role(raw.get("role", "assistant")),
            content=raw.get("content"),
            tool_calls=calls,
            tool_call_id=raw.get("tool_call_id"),
            name=raw.get("name"),
            reasoning=raw.get("reasoning_content") or raw.get("reasoning"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "reasoning": self.reasoning,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Message:
        return cls(
            role=Role(raw.get("role", "assistant")),
            content=raw.get("content"),
            tool_calls=[
                ToolCall(id=c.get("id") or new_id("call_"), name=c["name"], arguments=c.get("arguments") or "{}")
                for c in (raw.get("tool_calls") or [])
            ],
            tool_call_id=raw.get("tool_call_id"),
            name=raw.get("name"),
            reasoning=raw.get("reasoning"),
            meta=raw.get("meta") or {},
        )


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    llm_calls: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            llm_calls=self.llm_calls + other.llm_calls,
        )

    @classmethod
    def from_openai(cls, raw: dict[str, Any] | None) -> "Usage":
        raw = raw or {}
        details = raw.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            llm_calls=1,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "llm_calls": self.llm_calls,
        }


@dataclass
class StepRecord:
    """一次 Agent Loop 迭代的审计记录。"""

    index: int
    started_at: float
    finished_at: float = 0.0
    assistant_text: str | None = None
    tool_names: list[str] = field(default_factory=list)
    tool_ok: int = 0
    tool_failed: int = 0
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None

    @property
    def latency_ms(self) -> float:
        return round((self.finished_at - self.started_at) * 1000, 1) if self.finished_at else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "latency_ms": self.latency_ms,
            "assistant_text": self.assistant_text,
            "tool_names": self.tool_names,
            "tool_ok": self.tool_ok,
            "tool_failed": self.tool_failed,
            "usage": self.usage.to_dict(),
            "stop_reason": self.stop_reason,
        }


def now_ts() -> float:
    return time.time()
