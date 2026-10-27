"""验证运行环境：参数净化、max_tokens 上限、模型连通性。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opspilot.config import get_settings
from opspilot.core.loop import sanitize_tool_calls
from opspilot.core.types import Message, Role, ToolCall
from opspilot.llm import build_llm


def check_sanitize() -> None:
    calls = [
        ToolCall(name="python_analysis", arguments='{"code": "import pandas as pd\\nprint(1'),
        ToolCall(name="run_sql", arguments='{"sql": "SELECT 1"}'),
    ]
    fixed = sanitize_tool_calls(calls)
    print("sanitize[0] ->", fixed[0].arguments[:70])
    print("sanitize[1] ->", fixed[1].arguments)


async def check_llm() -> None:
    settings = get_settings()
    print("model =", settings.llm_model, "| max_tokens =", settings.llm_max_tokens)
    llm = build_llm(settings)
    try:
        resp = await llm.complete(
            [Message(role=Role.USER, content="回复两个字：收到")],
            max_tokens=settings.llm_max_tokens,
        )
        print("content =", repr(resp.content)[:60])
        print("usage   =", resp.usage.to_dict())
    finally:
        await llm.aclose()


if __name__ == "__main__":
    check_sanitize()
    asyncio.run(check_llm())
