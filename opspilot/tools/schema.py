"""数据源探查工具：表结构、字段类型、行数、时间覆盖范围、指标口径。

这是 Agent 的第一步"看地形"。没有它，模型只能猜表名，错误率会非常高。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from opspilot.domain.metrics import glossary_dict
from opspilot.harness.registry import ToolContext
from opspilot.tools._common import connect_readonly, jsonable, table_row_count


class InspectSchemaArgs(BaseModel):
    table: str | None = Field(
        default=None,
        description="要查看的表名；不填则返回所有表的概览。",
    )
    sample_rows: int = Field(default=3, ge=0, le=10, description="指定表时返回的样例行数。")


DESCRIPTION = (
    "查看数据仓库的表结构、字段类型、行数、时间范围、指标口径，以及关键枚举列的值域。"
    "不确定表名/字段名/枚举取值时先调用它；不要凭空猜测。"
)

# 这些列是"枚举型业务字段"，把值域直接给模型，能避免"猜值 → 空结果 → 再查"的往返
_ENUM_COLUMNS = {
    "energy_type",
    "unit",
    "shift",
    "severity",
    "equipment_type",
    "process",
    "workshop_id",
    "product_code",
    "alarm_code",
    "code",
}


def _column_values(con, table: str, column: str, limit: int = 12) -> list | None:
    try:
        rows = con.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
            f'ORDER BY 1 LIMIT {limit + 1}'
        ).fetchall()
    except Exception:  # noqa: BLE001 - 个别列可能不可直接比较，忽略即可
        return None
    if not rows or len(rows) > limit:
        return None
    return [jsonable(r[0]) for r in rows]


async def inspect_schema(args: InspectSchemaArgs, ctx: ToolContext) -> dict[str, Any]:
    con = connect_readonly(ctx.settings.db_path)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]

        if args.table:
            if args.table not in tables:
                return {
                    "ok": False,
                    "error": f"表 {args.table} 不存在",
                    "available_tables": tables,
                }
            return {"ok": True, **_describe_table(con, args.table, args.sample_rows)}

        overview = []
        for table in tables:
            info: dict[str, Any] = {
                "name": table,
                "rows": table_row_count(con, table),
                "columns": [
                    {"name": r[0], "type": r[1]}
                    for r in con.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = ? AND table_schema='main' ORDER BY ordinal_position",
                        (table,),
                    ).fetchall()
                ],
            }
            for col in info["columns"]:
                if col["name"] in _ENUM_COLUMNS:
                    values = _column_values(con, table, col["name"])
                    if values is not None:
                        col["values"] = values
            if "record_date" in {c["name"] for c in info["columns"]}:
                lo, hi = con.execute(f'SELECT MIN(record_date), MAX(record_date) FROM "{table}"').fetchone()
                info["date_range"] = [jsonable(lo), jsonable(hi)]
            overview.append(info)

        return {
            "ok": True,
            "today": "2026-10-27",
            "tables": overview,
            "metric_glossary": glossary_dict(),
            "hint": "枚举列的 values 已给出，请直接使用，不要猜测取值；车间编号形如 WS-03。",
        }
    finally:
        con.close()


def _describe_table(con, table: str, sample_rows: int) -> dict[str, Any]:
    columns = [
        {"name": r[0], "type": r[1], "nullable": r[2] == "YES"}
        for r in con.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema='main' ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    ]
    result: dict[str, Any] = {
        "table": table,
        "rows": table_row_count(con, table),
        "columns": columns,
    }
    for col in columns:
        if col["name"] in _ENUM_COLUMNS:
            values = _column_values(con, table, col["name"])
            if values is not None:
                col["values"] = values
    if sample_rows:
        cur = con.execute(f'SELECT * FROM "{table}" LIMIT {int(sample_rows)}')
        result["sample"] = {
            "columns": [d[0] for d in cur.description],
            "rows": [[jsonable(v) for v in row] for row in cur.fetchall()],
        }
    return result
