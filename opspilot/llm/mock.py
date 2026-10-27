"""确定性脚本模型：没有 API Key 也能完整跑通 Agent Loop 与全部测试。"""

from __future__ import annotations

import json
from typing import Any, Callable

from opspilot.core.types import Message, ToolCall, Usage
from opspilot.llm.base import BaseLLM, LLMResponse

ScriptItem = dict[str, Any] | LLMResponse | Callable[[list[Message], list[dict[str, Any]] | None], Any]


def tool_call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {
        "name": name,
        "arguments": args,
        "id": call_id or f"call_{name}_{abs(hash(args)) % 10000}",
    }


def assistant(content: str | None = None, *, tool_calls: list[dict[str, Any]] | None = None, reasoning: str | None = None) -> dict[str, Any]:
    return {"content": content, "tool_calls": tool_calls or [], "reasoning": reasoning}


class ScriptedLLM(BaseLLM):
    """按顺序消费脚本；脚本耗尽后返回一句收尾文本，避免死循环。"""

    def __init__(self, script: list[ScriptItem], *, model_name: str = "scripted-mock", repeat_last: bool = False) -> None:
        self.script = script
        self.model_name = model_name
        self.repeat_last = repeat_last
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        idx = len(self.calls) - 1
        if idx >= len(self.script):
            if not self.repeat_last or not self.script:
                return LLMResponse(content="[mock] 调查结束。", finish_reason="stop", model=self.model_name)
            idx = len(self.script) - 1
        item = self.script[idx]
        if callable(item) and not isinstance(item, LLMResponse):
            item = item(messages, tools)
        return self._coerce(item)

    def _coerce(self, item: Any) -> LLMResponse:
        if isinstance(item, LLMResponse):
            item.usage = item.usage.add(Usage(llm_calls=1))
            return item
        if isinstance(item, str):
            item = {"content": item}
        calls = [
            ToolCall(
                name=c["name"],
                arguments=c.get("arguments") if isinstance(c.get("arguments"), str) else json.dumps(c.get("arguments") or {}, ensure_ascii=False),
                id=c.get("id") or f"call_{i}",
            )
            for i, c in enumerate(item.get("tool_calls") or [])
        ]
        return LLMResponse(
            content=item.get("content"),
            tool_calls=calls,
            reasoning=item.get("reasoning"),
            finish_reason="tool_calls" if calls else "stop",
            model=self.model_name,
            usage=Usage(llm_calls=1, prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )


class EchoLLM(BaseLLM):
    """最小可用模型：永远直接回答，用于验证"无工具"分支。"""

    model_name = "echo-mock"

    async def complete(self, messages: list[Message], tools=None, *, temperature=None, max_tokens=None) -> LLMResponse:
        last = next((m for m in reversed(messages) if m.role.value == "user"), None)
        return LLMResponse(content=f"[echo] {last.content if last else ''}", finish_reason="stop", model=self.model_name, usage=Usage(llm_calls=1))
