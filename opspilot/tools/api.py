"""业务系统 API 工具（模拟 MES / 设备管理 / 生产系统）。

真实企业里这些系统往往由不同团队维护、参数口径不统一：
- 车间编号必须是 WS-03 而不是 "3号车间" 或 "3"；
- 日期必须是 YYYY-MM-DD，不接受 "2026/9/1" 或带时区的 ISO 串；
- 生产月报只接受 YYYY-MM。

参数格式问题由 Harness 在调用前自动修（见 executor.repair_arguments）；
语义问题（编号不存在、范围过大）由后端返回结构化 400，Agent 据此改参数重试。
这条"能修的程序修、不能修的交模型"的分界线，是本项目的核心设计之一。
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from opspilot.harness.errors import ErrorKind, ToolError
from opspilot.harness.registry import ToolContext


class EquipmentQueryArgs(BaseModel):
    workshop_id: str = Field(pattern=r"^WS-\d{2}$", description="车间编号，必须形如 WS-03")
    start_date: str = Field(description="开始日期 YYYY-MM-DD（含）")
    end_date: str = Field(description="结束日期 YYYY-MM-DD（含），跨度不超过 92 天")
    equipment_type: str | None = Field(default=None, description="可选：按设备类型过滤，如 空压机 / 风机")


class AlarmQueryArgs(BaseModel):
    workshop_id: str = Field(pattern=r"^WS-\d{2}$", description="车间编号，必须形如 WS-03")
    start_date: str = Field(description="开始日期 YYYY-MM-DD")
    end_date: str = Field(description="结束日期 YYYY-MM-DD，跨度不超过 92 天")
    severity: str | None = Field(default=None, description="可选：告警级别 高/中/低")


class ProductionSummaryArgs(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$", description="统计月份 YYYY-MM，如 2026-09")
    workshop_id: str | None = Field(default=None, pattern=r"^WS-\d{2}$", description="可选：车间编号")


DESCRIPTION_EQUIPMENT = (
    "调用设备管理系统 API，按车间与日期范围查询设备运行明细（运行/空转时长、负载率、温度、振动、电耗）。"
    "用于核对设备侧证据。"
)
DESCRIPTION_ALARM = "调用设备管理系统 API，按车间与日期范围查询设备告警记录（含告警码、级别、持续时长）。"
DESCRIPTION_PRODUCTION = "调用生产管理系统 API，查询指定月份的生产汇总（产量、计划量、不良数、完成率）。"


async def query_equipment_api(args: EquipmentQueryArgs, ctx: ToolContext) -> dict[str, Any]:
    return await _get(
        ctx,
        "/api/v1/equipment/operations",
        {
            "workshop_id": args.workshop_id,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "equipment_type": args.equipment_type,
        },
    )


async def query_alarm_api(args: AlarmQueryArgs, ctx: ToolContext) -> dict[str, Any]:
    return await _get(
        ctx,
        "/api/v1/equipment/alarms",
        {
            "workshop_id": args.workshop_id,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "severity": args.severity,
        },
    )


async def query_production_api(args: ProductionSummaryArgs, ctx: ToolContext) -> dict[str, Any]:
    return await _get(
        ctx,
        "/api/v1/production/summary",
        {"month": args.month, "workshop_id": args.workshop_id},
    )


async def _get(ctx: ToolContext, path: str, params: dict[str, Any]) -> dict[str, Any]:
    base = ctx.settings.business_api_base.rstrip("/")
    headers = {"Accept": "application/json"}
    if ctx.settings.business_api_token:
        headers["Authorization"] = f"Bearer {ctx.settings.business_api_token}"

    try:
        async with httpx.AsyncClient(base_url=base, timeout=30.0, trust_env=False) as client:
            resp = await client.get(path, params={k: v for k, v in params.items() if v is not None}, headers=headers)
    except httpx.ConnectError as exc:
        raise ToolError(
            ErrorKind.UPSTREAM_ERROR,
            f"业务系统不可达（{base}）。请确认 mock ERP 服务已启动。",
            hint="可运行 `python -m opspilot.mock_erp.app` 启动本地业务系统。",
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolError(ErrorKind.TIMEOUT, "业务系统响应超时", hint="缩小查询范围后重试") from exc

    if resp.status_code == 200:
        payload = resp.json()
        payload["_source"] = f"{path} @ {base}"
        return payload

    detail: Any = None
    message = f"业务系统返回 HTTP {resp.status_code}"
    hint = None
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            message = err.get("message") or message
            hint = err.get("hint")
            detail = err
        else:
            detail = body
            if isinstance(body, dict) and body.get("detail"):
                message = str(body["detail"])[:300]
    except ValueError:
        detail = resp.text[:800]

    kind = ErrorKind.INVALID_ARGUMENT if 400 <= resp.status_code < 500 else ErrorKind.UPSTREAM_ERROR
    raise ToolError(kind, message, hint=hint, details=detail)
