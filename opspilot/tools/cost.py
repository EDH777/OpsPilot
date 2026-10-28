"""成本查询工具：只有总经理角色可见。

这是轻量版权限体系里唯一“工具级”差异：车间管理员的工具 schema 里没有它，
即使模型幻觉调用，执行器也会拒绝。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from opspilot.harness.registry import ToolContext
from opspilot.security.data import connect_scoped

DESCRIPTION = (
    "查询月度成本与毛利（财务域）。只有总经理角色可用；"
    "返回 electricity_cost / total_cost / revenue / gross_margin / unit_price。"
    "成本为模拟数据，不构成真实财务结论。"
)


class CostQueryArgs(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$", description="统计月份 YYYY-MM")
    workshop_id: str | None = Field(
        default=None,
        pattern=r"^WS-\d{2}$",
        description="可选：车间编号；不填则查全厂",
    )


async def query_cost_summary(args: CostQueryArgs, ctx: ToolContext) -> dict[str, Any]:
    con = connect_scoped(ctx.settings.db_path, ctx.principal)
    try:
        sql = "SELECT month, workshop_id, electricity_cost, total_cost, revenue, gross_margin, unit_price FROM workshop_cost WHERE month = ?"
        params: list[Any] = [args.month]
        if args.workshop_id:
            sql += " AND workshop_id = ?"
            params.append(args.workshop_id)
        cur = con.execute(sql + " ORDER BY workshop_id", params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    finally:
        con.close()

    return {
        "ok": True,
        "month": args.month,
        "rows": [dict(zip(columns, row)) for row in rows],
        "note": "成本为合成数据，仅用于演示；不构成真实财务结论。",
    }
