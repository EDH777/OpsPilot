"""结构化报告提交工具：显式终止信号 + 强 Schema。

让模型"写一段 Markdown 总结"很难校验；让模型填结构化字段，可以：
- 强制区分 已验证事实 / 证据支持 / 待核实；
- 让 UI 直接渲染 KPI、证据链、建议；
- 让评测脚本能自动检查报告质量（是否带数字、是否夸大因果）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext


class Finding(BaseModel):
    statement: str = Field(description="结论，一句话，尽量带数字")
    evidence: str = Field(description="支撑该结论的具体证据：数值、时间范围、数据表/接口")
    confidence: Literal["已验证事实", "证据支持", "待核实"] = Field(
        description="已验证事实=查询直接给出；证据支持=多个事实指向但未排除其他原因；待核实=缺数据"
    )


class KeyMetric(BaseModel):
    name: str = Field(description="指标名，如 单位产品电耗")
    value: float | str = Field(description="数值")
    unit: str = Field(default="", description="单位，如 kWh/件")
    period: str = Field(default="", description="统计区间，如 2026-09-28~2026-10-20")


class SubmitReportArgs(BaseModel):
    title: str = Field(description="报告标题")
    summary: str = Field(description="3~5 句结论摘要，必须包含关键数字")
    metrics: list[KeyMetric] = Field(default_factory=list, description="关键指标卡片")
    findings: list[Finding] = Field(min_length=1, description="结论与证据条目，按重要性排序")
    recommendations: list[str] = Field(default_factory=list, description="可执行的改进建议")
    limitations: list[str] = Field(default_factory=list, description="调查局限与未排除因素")
    charts: list[str] = Field(default_factory=list, description="本次生成的图表文件名（python_analysis 返回的 artifacts）")


DESCRIPTION = (
    "提交最终的结构化调查报告。这是调查的收尾动作：证据充分后调用一次即可结束任务。"
    "报告中的数字必须来自前面的工具结果。"
)


async def submit_report(args: SubmitReportArgs, ctx: ToolContext) -> dict[str, Any]:
    if not args.findings:
        raise ToolError(ErrorKind.INVALID_ARGUMENT, "报告至少需要一条 findings 结论")
    if len(args.summary) < 20:
        raise ToolError(ErrorKind.INVALID_ARGUMENT, "summary 太短，请写出包含关键数字的结论摘要")

    payload = args.model_dump()
    ctx.shared["report"] = payload
    ctx.publish("report", report=payload)
    return {
        "accepted": True,
        "findings": len(args.findings),
        "message": "报告已接收，调查结束。",
    }
