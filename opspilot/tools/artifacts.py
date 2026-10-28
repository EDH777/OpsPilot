"""读取沙箱产物文本（stdout.txt / stderr.txt / csv 等），按行分页。

存在意义：Python 输出不再整段塞进上下文，完整内容落盘，模型需要时可主动翻页。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from opspilot.harness.registry import ToolContext
from opspilot.tools._common import resolve_artifact

_TEXT_SUFFIXES = {".txt", ".log", ".md", ".csv", ".json"}


class ReadArtifactArgs(BaseModel):
    path: str = Field(description="产物相对路径，例如 py_123456/stdout.txt")
    offset: int = Field(default=0, ge=0, description="起始行（0 基）")
    limit: int = Field(default=200, ge=1, le=500, description="读取行数，最多 500")


DESCRIPTION = (
    "读取沙箱产物文本（stdout.txt / stderr.txt / csv / json 等），按行分页返回。"
    "用于查看被截断的完整输出；不要一次拉取超大文件。"
)


async def read_artifact(args: ReadArtifactArgs, ctx: ToolContext) -> dict[str, Any]:
    path = resolve_artifact(ctx, args.path, _TEXT_SUFFIXES)

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    chunk = lines[args.offset : args.offset + args.limit]
    content = "\n".join(chunk)
    if len(content) > 20000:
        content = content[:20000] + "\n…（本次读取内容过长，已截断；请减小 limit）…"

    return {
        "ok": True,
        "path": args.path,
        "offset": args.offset,
        "limit": args.limit,
        "total_lines": total,
        "lines": content,
        "has_more": args.offset + len(chunk) < total,
    }
