"""权限模型：Principal、角色、数据域。

设计约束：
- 角色决定“能用哪些工具”，Principal.workshops 决定“能看哪些车间”；
- 表/列级策略是角色配置，不散落在业务代码里；
- 模型永远不是授权主体：所有判断的输入都是 Principal。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

BASE_TABLES = (
    "workshop",
    "production_record",
    "energy_record",
    "equipment",
    "equipment_operation",
    "equipment_alarm",
    "alarm_code",
)


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    role: str
    workshops: frozenset[str] | None = None  # None = 全厂

    def allows(self, workshop_id: str) -> bool:
        return self.workshops is None or workshop_id in self.workshops

    @property
    def scope_label(self) -> str:
        if self.workshops is None:
            return "全厂"
        return "、".join(sorted(self.workshops)) if self.workshops else "无业务数据"

    def as_scope(self) -> list[str] | None:
        return None if self.workshops is None else sorted(self.workshops)


@dataclass(frozen=True)
class Role:
    label: str
    tools: frozenset[str]
    tables: Mapping[str, tuple[str, ...] | None]
    row_scope: bool
    can_read_audit: bool = False


ROLES: dict[str, Role] = {
    "executive": Role(
        label="总经理",
        tools=frozenset({
            "inspect_schema",
            "run_sql",
            "read_result",
            "read_artifact",
            "query_equipment_api",
            "query_alarm_api",
            "query_production_api",
            "python_analysis",
            "submit_report",
            "query_cost_summary",
        }),
        tables={**{table: None for table in BASE_TABLES}, "workshop_cost": None},
        row_scope=False,
        can_read_audit=True,
    ),
    "workshop_manager": Role(
        label="车间管理员",
        tools=frozenset({
            "inspect_schema",
            "run_sql",
            "read_result",
            "read_artifact",
            "query_equipment_api",
            "query_alarm_api",
            "query_production_api",
            "python_analysis",
            "submit_report",
            # 刻意没有 query_cost_summary：成本是总经理域
        }),
        tables={
            "workshop": None,
            "production_record": None,
            "energy_record": (
                "record_date",
                "workshop_id",
                "energy_type",
                "quantity",
                "unit",
                # 刻意没有 cost_cny：成本字段对车间管理员不可见
            ),
            "equipment": None,
            "equipment_operation": None,
            "equipment_alarm": None,
            "alarm_code": None,
            # 刻意没有 workshop_cost：成本表对车间管理员不可见
        },
        row_scope=True,
        can_read_audit=False,
    ),
}


def role_of(principal: Principal) -> Role:
    """未知角色 fail closed。"""
    try:
        return ROLES[principal.role]
    except KeyError as exc:  # pragma: no cover - 正常路径不会发生
        raise PermissionError(f"未知角色：{principal.role}") from exc
