"""Python 代码执行工具：让 Agent 具备"没有现成工具时自己算"的能力。

代码在受控沙箱中运行（独立进程 / 超时 / 输出截断），可读取只读数据库、生成图表。
执行失败不抛异常，而是把 stderr 作为 Observation 返回，让模型自己看见 traceback 并修正——
这是"代码即工具"的关键：错误信息本身也是上下文。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from opspilot.harness.registry import ToolContext
from opspilot.harness.sandbox import PythonSandbox


class PythonAnalysisArgs(BaseModel):
    code: str = Field(
        description=(
            "完整可执行的 Python 代码。可用 pandas / numpy / matplotlib / duckdb。"
            "数据库路径在环境变量 OPSPILOT_DB_PATH 中，请用 duckdb.connect(..., read_only=True) 打开。"
            "图表必须 plt.savefig('xxx.png', dpi=120) 到当前目录，并用 print() 输出关键数字。"
        )
    )
    purpose: str | None = Field(default=None, description="这段代码要计算或验证什么，用于审计。")


DESCRIPTION = (
    "在隔离沙箱中执行 Python 代码，用于复杂计算、统计检验与绘图（保存 png 后会自动登记为报告图表）。"
    "代码无网络访问；stdout/stderr 会返回，出错可根据 traceback 修正后重试。"
    "读取数据库必须用：import os, duckdb; "
    "con = duckdb.connect(os.environ['OPSPILOT_DB_PATH'], read_only=True)。"
    "不要硬编码任何数据库路径，也不要用 /home、/tmp 等 Linux 路径。"
)


async def python_analysis(args: PythonAnalysisArgs, ctx: ToolContext) -> dict[str, Any]:
    sandbox = PythonSandbox(ctx.settings)
    result = await sandbox.run(args.code, artifacts_dir=ctx.artifacts_dir)
    data = result.to_dict()
    data["purpose"] = args.purpose
    if not result.ok:
        data["hint"] = "请阅读 stderr 中的 traceback，修正代码后重试；不要重复提交同样的代码。"
    return data
