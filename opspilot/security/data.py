"""数据权限：按角色裁剪的 DuckDB 视图、SQL guard、Python 快照。

主边界是视图：模型只能看到过滤后的行和允许的列。
词法 guard 只是防旁路（raw./main./read_parquet/越界车间字面量），
不是完整 SQL 防火墙；生产可替换为 sqlglot AST，接口不变。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.security.model import ROLES, Principal

# 带 workshop_id 的表：按 principal 的车间域过滤
ROW_SCOPED = {
    "workshop",
    "production_record",
    "energy_record",
    "equipment",
    "equipment_operation",
    "equipment_alarm",
    "workshop_cost",
}

_READ_PREFIXES = (
    "select",
    "with",
    "describe",
    "desc",
    "show",
    "summarize",
    "explain",
    "table",
)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|"
    r"pragma|truncate|grant|revoke|vacuum|checkpoint|call|merge|upsert)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_SCHEMA_QUALIFIED = re.compile(
    r'["\']?(raw|main|information_schema|pg_catalog|temp)["\']?\s*\.',
    re.IGNORECASE,
)
_TABLE_FUNCTION = re.compile(
    r"\b(read_parquet|read_csv|read_json|read_csv_auto|read_json_auto|read_text|read_blob|"
    r"parquet_scan|parquet_metadata|parquet_schema|csv_scan|glob|query_table|query|"
    r"duckdb_\w+|pragma_\w+)\s*\(",
    re.IGNORECASE,
)
# DuckDB 支持 FROM 'file.csv' 这种 replacement scan；只允许 FROM 标识符/子查询。
_FILE_SCAN = re.compile(r"\b(from|join)\s*'", re.IGNORECASE)
_WS = re.compile(r"WS-\d{2}")


def validate_readonly_sql(raw: str) -> str:
    """原有词法层：只读、单语句、禁写/DDL/导出。"""
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


def guard_scoped_sql(sql: str, principal: Principal) -> str:
    """只读 + 禁止 schema 限定/外部函数 + 禁止越界车间字面量。"""
    body = validate_readonly_sql(sql)

    if _SCHEMA_QUALIFIED.search(body):
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            "不允许使用 raw/main/information_schema 等限定名",
            hint="请直接查询不带 schema 前缀的表名；inspect_schema 会给出可用表。",
        )
    if _TABLE_FUNCTION.search(body) or _FILE_SCAN.search(body):
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            "不允许读取外部文件或系统表函数",
            hint="只允许查询当前角色的业务视图。",
        )
    for workshop in _WS.findall(body):
        if not principal.allows(workshop):
            raise ToolError(
                ErrorKind.PERMISSION_DENIED,
                "只能分析你负责的车间",
                hint="请只查询自己数据域内的车间；不要引用其他车间编号。",
            )
    return body


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _select_list(columns: tuple[str, ...] | None) -> str:
    return "*" if columns is None else ", ".join(_q(c) for c in columns)


def _where(principal: Principal, table: str, row_scope: bool) -> str:
    if not row_scope or table not in ROW_SCOPED:
        return ""
    workshops = sorted(principal.workshops or [])
    if not workshops:
        return " WHERE 1=0"
    return " WHERE workshop_id IN (" + ", ".join(_lit(w) for w in workshops) + ")"


def connect_scoped(db_path: Path, principal: Principal):
    """每个请求/run 一个 in-memory DB，只暴露按角色裁剪的视图。"""
    role = ROLES[principal.role]
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH {_lit(db_path)} AS raw (READ_ONLY)")
    for table, columns in role.tables.items():
        con.execute(
            f"CREATE TEMP VIEW {_q(table)} AS "
            f"SELECT {_select_list(columns)} FROM raw.{_q(table)}"
            f"{_where(principal, table, role.row_scope)}"
        )
    try:
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=2")
    except Exception:  # noqa: BLE001 - 低版本 duckdb 不支持也不影响
        pass
    return con


def ensure_scoped_snapshot(ctx: Any) -> Path:
    """给 Python 沙箱的授权快照：一个只含当前角色可见数据的 DuckDB 文件。"""
    path = Path(ctx.artifacts_dir) / "scoped.duckdb"
    if path.exists():
        return path

    role = ROLES[ctx.principal.role]
    con = duckdb.connect(str(path))
    try:
        con.execute(f"ATTACH {_lit(ctx.settings.db_path)} AS raw (READ_ONLY)")
        for table, columns in role.tables.items():
            con.execute(
                f"CREATE TABLE {_q(table)} AS "
                f"SELECT {_select_list(columns)} FROM raw.{_q(table)}"
                f"{_where(ctx.principal, table, role.row_scope)}"
            )
    finally:
        con.close()
    return path
