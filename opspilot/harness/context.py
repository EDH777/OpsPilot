"""上下文管理：Token 预算 + 工具输出截断 + 历史压缩。

LLM 的上下文不是垃圾桶。长 SQL 结果、几十轮工具输出会把窗口塞满并拖慢推理，
所以这里做两件事：单条工具输出保头保尾截断；整体超预算时压缩早期步骤为一条摘要。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from opspilot.core.types import Message, Role, estimate_tokens

_REF_PATTERN = re.compile(r'"(result_ref|stdout_ref|stderr_ref)":\s*"([^"]+)"')


@dataclass
class CompactionStats:
    original_tokens: int = 0
    final_tokens: int = 0
    truncated_outputs: int = 0
    dropped_messages: int = 0
    compacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "final_tokens": self.final_tokens,
            "truncated_outputs": self.truncated_outputs,
            "dropped_messages": self.dropped_messages,
            "compacted": self.compacted,
        }


def message_tokens(msg: Message) -> int:
    payload = msg.content or ""
    if msg.tool_calls:
        payload += json.dumps([c.to_openai() for c in msg.tool_calls], ensure_ascii=False)
    return estimate_tokens(payload) + 4


class ContextManager:
    def __init__(
        self,
        *,
        token_budget: int = 40_000,
        keep_tail: int = 8,
        tool_output_chars: int = 4000,
        compact_ratio: float = 0.72,
    ) -> None:
        self.token_budget = token_budget
        self.keep_tail = keep_tail
        self.tool_output_chars = tool_output_chars
        self.compact_ratio = compact_ratio

    def compact(self, messages: list[Message]) -> tuple[list[Message], CompactionStats]:
        stats = CompactionStats()
        working = [m for m in messages]
        stats.original_tokens = sum(message_tokens(m) for m in working)

        # 1) 单条工具输出截断（保头 60% + 保尾 40%，中间标注省略）
        working, truncated = self._truncate_tool_outputs(working)
        stats.truncated_outputs = truncated
        tokens = sum(message_tokens(m) for m in working)

        # 2) 整体超预算：保留 system + 初始任务 + 最近若干条，压缩中间历史
        threshold = int(self.token_budget * self.compact_ratio)
        if tokens > threshold:
            working, dropped = self._drop_middle(working)
            stats.dropped_messages = dropped
            stats.compacted = dropped > 0
            tokens = sum(message_tokens(m) for m in working)

        # 3) 仍然超预算：进一步收紧工具输出
        if tokens > self.token_budget and len(working) > 2:
            working, extra = self._truncate_tool_outputs(working, limit=900)
            stats.truncated_outputs += extra
            tokens = sum(message_tokens(m) for m in working)

        stats.final_tokens = tokens
        return working, stats

    # ------------------------------------------------------------------ 内部

    def _truncate_tool_outputs(self, messages: list[Message], limit: int | None = None) -> tuple[list[Message], int]:
        limit = limit or self.tool_output_chars
        out: list[Message] = []
        count = 0
        for msg in messages:
            if msg.role is Role.TOOL and msg.content and len(msg.content) > limit:
                content = msg.content
                head = int(limit * 0.6)
                tail = limit - head
                refs: list[str] = []
                for _, ref in _REF_PATTERN.findall(content):
                    if ref not in refs:
                        refs.append(ref)
                ref_text = f"；完整结果见 {'、'.join(refs)}，可用 read_result/read_artifact 读取" if refs else ""
                msg = Message(
                    role=msg.role,
                    content=(
                        content[:head]
                        + f"\n…（harness 截断 {len(content) - head - tail} 字符{ref_text}）…\n"
                        + content[-tail:]
                    ),
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    meta={**msg.meta, "truncated_from": len(content)},
                )
                count += 1
            out.append(msg)
        return out, count

    def _drop_middle(self, messages: list[Message]) -> tuple[list[Message], int]:
        if len(messages) <= self.keep_tail + 2:
            return messages, 0

        head = messages[:2]  # system + 初始任务
        tail = messages[-self.keep_tail :]
        middle = messages[2 : len(messages) - self.keep_tail]
        if not middle:
            return messages, 0

        lines: list[str] = []
        for msg in middle:
            if msg.role is Role.ASSISTANT and msg.tool_calls:
                for call in msg.tool_calls:
                    lines.append(f"· 调用 {call.name}（参数 {call.arguments[:120]}）")
            elif msg.role is Role.TOOL:
                snippet = (msg.content or "").replace("\n", " ")[:160]
                refs = [ref for _, ref in _REF_PATTERN.findall(msg.content or "")]
                if refs:
                    snippet += f"（完整结果：{refs[0]}）"
                lines.append(f"  → 结果：{snippet}")
            elif msg.role is Role.ASSISTANT and msg.content:
                lines.append(f"· 说明：{msg.content[:160]}")
        summary = (
            f"【Harness 上下文压缩】为控制 token，已省略 {len(middle)} 条早期消息。"
            "要点如下（如需细节请重新调用工具查询）：\n" + "\n".join(lines[-40:])
        )
        compacted = Message(role=Role.SYSTEM, content=summary, name="harness_summary")
        return head + [compacted] + tail, len(middle)
