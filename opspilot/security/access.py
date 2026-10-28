"""访问控制辅助：参数 scope 收敛 + 对象 owner 校验。

注意：这里刻意不做“从自然语言任务里猜车间”的预检。
任务文本不是可靠的授权输入；越界请求应交给工具层在真实调用时拒绝，
这样既不会误伤“一号/1号/一号线”等表达，也不会把权限判断耦合进 Agent Loop。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.security.model import Principal


SCOPE_FIELDS = {
    "query_equipment_api": "workshop_id",
    "query_alarm_api": "workshop_id",
    "query_production_api": "workshop_id",
    "query_cost_summary": "workshop_id",
}


def bind_scope(tool: str, args: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """把模型给的车间参数收敛到 Principal 的数据域内。

    放在执行器 repair 之后、Pydantic validate 之前：
    - 单车间角色没传 workshop_id -> 自动填入；
    - 传了越界车间 -> 直接拒绝；
    - 全厂角色不传 -> 允许查全厂。
    """
    field = SCOPE_FIELDS.get(tool)
    if not field:
        return args

    requested = args.get(field)
    if requested is None:
        if principal.workshops and len(principal.workshops) == 1:
            args[field] = next(iter(principal.workshops))
    elif not principal.allows(str(requested)):
        raise ToolError(
            ErrorKind.PERMISSION_DENIED,
            "该车间不在你的数据权限内",
            hint="请只查询自己负责的车间。",
        )
    return args


def require_run(store: Any, run_id: str, principal: Principal) -> dict[str, Any]:
    """owner 校验；不存在与无权都返回 404，避免枚举。"""
    row = store.load(run_id)
    if not row or row.get("owner_user_id") != principal.user_id:
        raise HTTPException(404, "run 不存在")
    return row


def require_session(store: Any, session_id: str, principal: Principal) -> list[dict[str, Any]]:
    rows = store.session_runs(session_id, owner_user_id=principal.user_id)
    if not rows:
        raise HTTPException(404, "会话不存在")
    return rows
