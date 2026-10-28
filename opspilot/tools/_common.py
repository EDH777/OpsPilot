"""工具层公共辅助：只读连接、结果序列化、产物路径校验。"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext


def connect_readonly(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=2")
    except Exception:  # noqa: BLE001 - 低版本 duckdb 可能不支持，不影响查询
        pass
    return con


def resolve_artifact(ctx: ToolContext, ref: str, suffixes: set[str]) -> Path:
    """把模型给的相对路径收敛到当前 run 的 artifacts 目录内。"""
    base = ctx.artifacts_dir.resolve()
    path = (base / ref).resolve()
    if not path.is_relative_to(base):
        raise ToolError(ErrorKind.PERMISSION_DENIED, "产物路径越界", hint="只能读取本 run 的产物")
    if path.suffix.lower() not in suffixes:
        allowed = " / ".join(sorted(suffixes))
        raise ToolError(ErrorKind.INVALID_ARGUMENT, f"不支持的文件类型，仅支持 {allowed}")
    if not path.is_file():
        raise ToolError(ErrorKind.NOT_FOUND, f"产物不存在：{ref}", hint="先确认工具返回的 artifacts / ref")
    return path


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else round(value, 6)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if hasattr(value, "item"):  # numpy 标量
        return jsonable(value.item())
    return str(value)


def table_row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
