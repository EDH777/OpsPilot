"""运行预算：步数 / 工具调用数 / Token / 墙钟时间四道硬闸。

任何一道超限都会以结构化原因终止循环，而不是让 Agent 静默跑飞。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from opspilot.core.types import Usage


class BudgetExceeded(RuntimeError):
    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit
        self.message = message


@dataclass
class Budget:
    max_steps: int = 24
    max_tool_calls: int = 40
    max_tokens: int = 120_000
    max_seconds: float = 600.0

    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # ---- 消耗 ----

    def tick_step(self) -> None:
        if self.steps >= self.max_steps:
            raise BudgetExceeded("max_steps", f"已达到最大推理步数 {self.max_steps}")
        self.steps += 1
        self.check_time()

    def reserve_tool(self, count: int = 1) -> None:
        if self.tool_calls + count > self.max_tool_calls:
            raise BudgetExceeded("max_tool_calls", f"工具调用数将超过上限 {self.max_tool_calls}")
        self.tool_calls += count

    def add_usage(self, usage: Usage) -> None:
        self.tokens += usage.total_tokens
        if self.tokens > self.max_tokens:
            raise BudgetExceeded("max_tokens", f"Token 消耗已超过上限 {self.max_tokens}")

    def check_time(self) -> None:
        elapsed = time.monotonic() - self.started_at
        if elapsed > self.max_seconds:
            raise BudgetExceeded("max_seconds", f"运行时间已超过 {self.max_seconds:.0f}s")

    # ---- 观测 ----

    @property
    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.started_at, 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "tokens": self.tokens,
            "max_tokens": self.max_tokens,
            "elapsed_s": self.elapsed_s,
            "max_seconds": self.max_seconds,
        }
