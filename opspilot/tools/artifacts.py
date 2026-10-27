"""读取沙箱产物文本（stdout.txt / stderr.txt / csv 等），按行分页。

存在意义：Python 输出不再整段塞进上下文，完整内容落盘，模型需要时可主动翻页。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext

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
    base = ctx.artifacts_dir.resolve()
    path = (base / args.path).resolve()
    if not path.is_relative_to(base):
        raise ToolError(ErrorKind.PERMISSION_DENIED, "产物路径越界", hint="只能读取本 run 的产物")
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        raise ToolError(ErrorKind.INVALID_ARGUMENT, f"不支持读取 {path.suffix} 类型，仅支持文本产物")
    if not path.is_file():
        raise ToolError(ErrorKind.NOT_FOUND, f"产物不存在：{args.path}", hint="先确认 python_analysis 返回的 artifacts/ref")

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
