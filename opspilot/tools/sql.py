"""只读 SQL 工具。

三层防护：SQL 词法白名单 → DuckDB 只读连接 → 行数上限 + 超时中断。
即使模型被骗着写 DELETE，也执行不了、跑不远。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext
from opspilot.tools._common import connect_readonly, jsonable

_READ_PREFIXES = ("select", "with", "describe", "desc", "show", "summarize", "explain", "table")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|"
    r"pragma|truncate|grant|revoke|vacuum|checkpoint|call|merge|upsert)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class RunSqlArgs(BaseModel):
    sql: str = Field(description="单条只读 SQL（SELECT / WITH ... SELECT）。不要加分号拼接多条语句。")
    purpose: str | None = Field(default=None, description="本次查询要回答的问题，用于审计与报告引用。")


DESCRIPTION = (
    "在只读数据仓库中执行一条 SQL 并返回结果（最多 500 行）。"
    "适合聚合、分组、关联、窗口计算；复杂统计或画图请用 python_analysis。"
)


def validate_readonly_sql(raw: str) -> str:
    sql = _COMMENT.sub(" ", raw or "").strip()
    if not sql:
        raise ToolError(ErrorKind.INVALID_ARGUMENT, "SQL 为空", hint="请提供一条 SELECT 查询")

    body = sql.rstrip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            "一次只允许执行一条语句",
            hint="请把多条语句拆成多次工具调用",
        )

    head = body.lstrip("( \t\r\n").lower()
    if not head.startswith(_READ_PREFIXES):
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            "只允许只读查询（SELECT / WITH / DESCRIBE / SHOW）",
            hint="本环境禁止写入、DDL 与数据导出；请改为查询。",
        )

    match = _FORBIDDEN.search(body)
    if match:
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            f"SQL 包含被禁止的关键字：{match.group(1).upper()}",
            hint="这里是只读分析环境，不支持修改数据或结构。",
        )
    return body


async def run_sql(args: RunSqlArgs, ctx: ToolContext) -> dict[str, Any]:
    sql = validate_readonly_sql(args.sql)
    max_rows = ctx.settings.sql_max_rows
    timeout = ctx.settings.sql_timeout_s

    con = connect_readonly(ctx.settings.db_path)
    started = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _execute, con, sql, max_rows)
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            con.interrupt()
            raise ToolError(
                ErrorKind.TIMEOUT,
                f"SQL 执行超过 {timeout:.0f}s 被中断",
                hint="请缩小时间范围、先做聚合再取明细，或加 WHERE 条件。",
            ) from None
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "ok": True,
        "sql": sql,
        "purpose": args.purpose,
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
        "elapsed_ms": elapsed_ms,
        "note": "结果已按 500 行上限截断" if result["truncated"] else None,
    }


def _execute(con, sql: str, max_rows: int) -> dict[str, Any]:
    cur = con.execute(sql)
    if cur.description is None:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    columns = [d[0] for d in cur.description]
    fetched = cur.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [[jsonable(v) for v in row] for row in fetched[:max_rows]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
