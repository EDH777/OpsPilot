"""内置工具装配。所有工具都通过注册表接入，Agent Loop 不感知具体实现。"""

from __future__ import annotations

from opspilot.harness.registry import ToolRegistry, ToolSpec
from opspilot.tools import api, artifacts, python_tool, report, schema, sql


def register_all(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="inspect_schema",
            description=schema.DESCRIPTION,
            args_model=schema.InspectSchemaArgs,
            handler=schema.inspect_schema,
            read_only=True,
            timeout_s=30,
            max_retries=1,
            tags=("meta",),
            examples=("查看所有表", "查看 energy_record 的字段"),
        )
    )
    registry.register(
        ToolSpec(
            name="run_sql",
            description=sql.DESCRIPTION,
            args_model=sql.RunSqlArgs,
            handler=sql.run_sql,
            read_only=True,
            timeout_s=30,
            max_retries=0,
            tags=("data",),
            examples=(
                "SELECT workshop_id, SUM(output_qty) FROM production_record GROUP BY 1",
                "WITH x AS (...) SELECT ...",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="read_result",
            description=sql.READ_RESULT_DESCRIPTION,
            args_model=sql.ReadResultArgs,
            handler=sql.read_result,
            read_only=True,
            timeout_s=30,
            max_retries=1,
            tags=("data", "artifact"),
        )
    )
    registry.register(
        ToolSpec(
            name="read_artifact",
            description=artifacts.DESCRIPTION,
            args_model=artifacts.ReadArtifactArgs,
            handler=artifacts.read_artifact,
            read_only=True,
            timeout_s=30,
            max_retries=1,
            tags=("artifact",),
        )
    )
    registry.register(
        ToolSpec(
            name="query_equipment_api",
            description=api.DESCRIPTION_EQUIPMENT,
            args_model=api.EquipmentQueryArgs,
            handler=api.query_equipment_api,
            read_only=True,
            timeout_s=30,
            max_retries=2,
            tags=("api", "equipment"),
        )
    )
    registry.register(
        ToolSpec(
            name="query_alarm_api",
            description=api.DESCRIPTION_ALARM,
            args_model=api.AlarmQueryArgs,
            handler=api.query_alarm_api,
            read_only=True,
            timeout_s=30,
            max_retries=2,
            tags=("api", "equipment"),
        )
    )
    registry.register(
        ToolSpec(
            name="query_production_api",
            description=api.DESCRIPTION_PRODUCTION,
            args_model=api.ProductionSummaryArgs,
            handler=api.query_production_api,
            read_only=True,
            timeout_s=30,
            max_retries=2,
            tags=("api", "production"),
        )
    )
    registry.register(
        ToolSpec(
            name="python_analysis",
            description=python_tool.DESCRIPTION,
            args_model=python_tool.PythonAnalysisArgs,
            handler=python_tool.python_analysis,
            read_only=True,
            timeout_s=120,
            max_retries=0,
            tags=("code", "sandbox"),
        )
    )
    registry.register(
        ToolSpec(
            name="submit_report",
            description=report.DESCRIPTION,
            args_model=report.SubmitReportArgs,
            handler=report.submit_report,
            read_only=True,
            timeout_s=10,
            max_retries=0,
            tags=("finish",),
        )
    )
    return registry


__all__ = ["register_all"]
