"""只读 SQL 工具。

三层防护：SQL 词法白名单 → DuckDB 只读连接 → 行数上限 + 超时中断。

结果策略（避免"静默截断"）：
- 小结果（≤ sql_summary_threshold）原样返回；
- 大结果落盘为 parquet，上下文只放：列、总行数、数值统计、低基数列 top 值、头尾样本、result_ref；
  模型需要明细时用 read_result(result_ref, offset, limit) 翻页。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext
from opspilot.security.data import connect_scoped, guard_scoped_sql
from opspilot.tools._common import jsonable, resolve_artifact

_NUMERIC_HINTS = ("INT", "DOUBLE", "DECIMAL", "FLOAT", "REAL", "NUMERIC", "HUGEINT")


class RunSqlArgs(BaseModel):
    sql: str = Field(description="单条只读 SQL（SELECT / WITH ... SELECT）。不要加分号拼接多条语句。")
    purpose: str | None = Field(default=None, description="本次查询要回答的问题，用于审计与报告引用。")


class ReadResultArgs(BaseModel):
    result_ref: str = Field(description="run_sql 返回的 result_ref，例如 results/r123.parquet")
    offset: int = Field(default=0, ge=0, description="从第几行开始（0 基）")
    limit: int = Field(default=50, ge=1, le=200, description="本次读取行数（最多 200）")


DESCRIPTION = (
    "在只读数据仓库中执行一条 SQL。结果 ≤50 行时直接返回；更大的结果会自动落盘为 parquet，"
    "返回列、总行数、数值统计、低基数列 top 值、头尾样本与 result_ref，"
    "需要明细时再用 read_result(result_ref, offset, limit) 翻页。"
    "适合聚合、分组、关联、窗口计算；复杂统计或画图请用 python_analysis。"
)

READ_RESULT_DESCRIPTION = (
    "读取 run_sql 落盘的大结果集，按 offset/limit 翻页，返回明细行与总行数。"
    "不要用它拉全量；先看统计再定位需要的行。"
)


async def run_sql(args: RunSqlArgs, ctx: ToolContext) -> dict[str, Any]:
    sql = guard_scoped_sql(args.sql, ctx.principal)
    threshold = ctx.settings.sql_summary_threshold
    preview = ctx.settings.sql_preview_rows
    timeout = ctx.settings.sql_timeout_s
    results_dir = ctx.artifacts_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    con = connect_scoped(ctx.settings.db_path, ctx.principal)
    started = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _execute, con, sql, threshold, preview, results_dir, ctx.artifacts_dir)
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
    payload: dict[str, Any] = {
        "ok": True,
        "sql": sql,
        "purpose": args.purpose,
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "returned_rows": len(result["rows"]),
        "truncated": result["truncated"],
        "sample": result.get("sample"),
        "stats": result.get("stats") or {},
        "top_values": result.get("top_values") or {},
        "result_ref": result.get("result_ref"),
        "elapsed_ms": elapsed_ms,
    }
    if result.get("result_ref"):
        payload["note"] = (
            f"完整结果（{result['row_count']} 行）已保存为 {result['result_ref']}；"
            "需要明细时调用 read_result(result_ref, offset, limit) 翻页，不要假设样本就是全部。"
        )
    elif result["truncated"]:
        payload["note"] = "结果已截断（当前查询类型不支持落盘）；请加聚合或 WHERE 缩小范围。"
    return payload


def _execute(
    con,
    sql: str,
    threshold: int,
    preview: int,
    results_dir: Path,
    artifacts_dir: Path,
) -> dict[str, Any]:
    cur = con.execute(sql)
    if cur.description is None:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    columns = [d[0] for d in cur.description]
    probe = cur.fetchmany(threshold + 1)
    if len(probe) <= threshold:
        return {
            "columns": columns,
            "rows": [[jsonable(v) for v in row] for row in probe],
            "row_count": len(probe),
            "truncated": False,
        }

    # 大结果：落盘（只对 SELECT/WITH 安全；元数据语句回退为截断）
    if not sql.lstrip().lower().startswith(("select", "with")):
        return {
            "columns": columns,
            "rows": [[jsonable(v) for v in row] for row in probe[:threshold]],
            "row_count": len(probe[:threshold]),
            "truncated": True,
        }

    filename = f"r{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.parquet"
    path = results_dir / filename
    literal = _sql_literal(path)
    con.execute(f"COPY ({sql}) TO {literal} (FORMAT PARQUET)")

    total = int(con.execute(f"SELECT COUNT(*) FROM read_parquet({literal})").fetchone()[0])
    head_n = max(1, int(preview * 0.75))
    tail_n = max(1, preview - head_n)
    head = con.execute(f"SELECT * FROM read_parquet({literal}) LIMIT {head_n}").fetchall()
    tail = con.execute(
        f"SELECT * FROM read_parquet({literal}) LIMIT {tail_n} OFFSET {max(0, total - tail_n)}"
    ).fetchall()

    described = con.execute(f"DESCRIBE SELECT * FROM read_parquet({literal})").fetchall()
    types = [(row[0], str(row[1])) for row in described]
    stats = _numeric_stats(con, literal, types)
    top_values = _top_values(con, literal, types)

    ref = path.relative_to(artifacts_dir).as_posix()
    return {
        "columns": columns,
        "rows": [[jsonable(v) for v in row] for row in head],
        "row_count": total,
        "truncated": True,
        "sample": {
            "head": [[jsonable(v) for v in row] for row in head],
            "tail": [[jsonable(v) for v in row] for row in tail],
        },
        "stats": stats,
        "top_values": top_values,
        "result_ref": ref,
    }


def _sql_literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_numeric(type_name: str) -> bool:
    upper = type_name.upper()
    return any(hint in upper for hint in _NUMERIC_HINTS)


def _numeric_stats(con, literal: str, types: list[tuple[str, str]]) -> dict[str, Any]:
    numeric = [(name, typ) for name, typ in types if _is_numeric(typ)]
    if not numeric:
        return {}

    parts: list[str] = []
    for name, _ in numeric:
        col = _quote(name)
        alias = name.replace('"', "")
        parts.extend(
            [
                f'MIN({col}) AS "{alias}__min"',
                f'MAX({col}) AS "{alias}__max"',
                f'AVG({col}) AS "{alias}__avg"',
                f'approx_quantile({col}, 0.5) AS "{alias}__p50"',
                f'approx_quantile({col}, 0.95) AS "{alias}__p95"',
                f'COUNT(*) - COUNT({col}) AS "{alias}__nulls"',
            ]
        )
    row = con.execute(f"SELECT {', '.join(parts)} FROM read_parquet({literal})").fetchone()
    stats: dict[str, Any] = {}
    index = 0
    for name, _ in numeric:
        stats[name] = {
            "min": jsonable(row[index]),
            "max": jsonable(row[index + 1]),
            "avg": jsonable(row[index + 2]),
            "p50": jsonable(row[index + 3]),
            "p95": jsonable(row[index + 4]),
            "nulls": jsonable(row[index + 5]),
        }
        index += 6
    return stats


def _top_values(con, literal: str, types: list[tuple[str, str]]) -> dict[str, Any]:
    text_cols = [name for name, typ in types if "CHAR" in typ.upper() or "STRING" in typ.upper()]
    result: dict[str, Any] = {}
    for name in text_cols[:2]:
        col = _quote(name)
        rows = con.execute(
            f"SELECT {col} AS value, COUNT(*) AS n FROM read_parquet({literal}) "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
        ).fetchall()
        result[name] = [{"value": jsonable(value), "count": int(count)} for value, count in rows]
    return result


async def read_result(args: ReadResultArgs, ctx: ToolContext) -> dict[str, Any]:
    path = resolve_artifact(ctx, args.result_ref, {".parquet"})
    con = duckdb.connect()
    try:
        literal = _sql_literal(path)
        total = int(con.execute(f"SELECT COUNT(*) FROM read_parquet({literal})").fetchone()[0])
        cur = con.execute(
            f"SELECT * FROM read_parquet({literal}) LIMIT {args.limit} OFFSET {args.offset}"
        )
        columns = [d[0] for d in cur.description]
        rows = [[jsonable(v) for v in row] for row in cur.fetchall()]
    finally:
        con.close()

    return {
        "ok": True,
        "result_ref": args.result_ref,
        "columns": columns,
        "rows": rows,
        "offset": args.offset,
        "limit": args.limit,
        "row_count": total,
        "has_more": args.offset + len(rows) < total,
    }
