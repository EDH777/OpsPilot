"""运行事件模型。事件是 UI 流式渲染、Trace 落盘与回放的统一载体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from opspilot.core.types import now_ts


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_RETRY = "tool_retry"
    ARG_REPAIRED = "arg_repaired"
    CONTEXT_COMPACTED = "context_compacted"
    CHECKPOINT = "checkpoint"
    REPORT = "report"
    RUN_FINISHED = "run_finished"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"


@dataclass
class Event:
    seq: int
    type: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "run_id": self.run_id,
            "ts": round(self.ts, 3),
            "data": self.data,
        }
