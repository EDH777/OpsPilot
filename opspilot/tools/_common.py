"""工具层公共辅助：只读连接、结果序列化。"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb


def connect_readonly(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=2")
    except Exception:  # noqa: BLE001 - 低版本 duckdb 可能不支持，不影响查询
        pass
    return con


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
