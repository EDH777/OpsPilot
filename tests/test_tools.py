"""工具层测试：只读 SQL、元数据探查、Python 沙箱、报告 Schema。"""

from __future__ import annotations

from pathlib import Path

import pytest

from opspilot.harness.errors import ErrorKind
from opspilot.harness.registry import ToolContext
from opspilot.tools import api, python_tool, report, schema, sql


@pytest.fixture
def ctx(settings, tmp_path: Path) -> ToolContext:
    ctx = ToolContext(run_id="run_tools", settings=settings, artifacts_dir=tmp_path / "artifacts")
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return ctx


async def test_inspect_schema_lists_tables(ctx):
    result = await schema.inspect_schema(schema.InspectSchemaArgs(), ctx)
    assert result["ok"] is True
    names = {t["name"] for t in result["tables"]}
    assert {"production_record", "energy_record", "equipment_operation"} <= names
    assert any(m["key"] == "unit_energy" for m in result["metric_glossary"])


async def test_inspect_schema_includes_enum_values(ctx):
    result = await schema.inspect_schema(schema.InspectSchemaArgs(), ctx)
    energy = next(t for t in result["tables"] if t["name"] == "energy_record")
    col = next(c for c in energy["columns"] if c["name"] == "energy_type")
    assert "values" in col
    assert "electricity" in col["values"]
    assert "natural_gas" in col["values"]


async def test_inspect_schema_single_table(ctx):
    result = await schema.inspect_schema(schema.InspectSchemaArgs(table="energy_record"), ctx)
    assert result["ok"] is True
    assert result["table"] == "energy_record"
    assert result["sample"]["columns"]


async def test_run_sql_returns_columns_and_rows(ctx):
    result = await sql.run_sql(
        sql.RunSqlArgs(sql="SELECT workshop_id, COUNT(*) AS n FROM workshop GROUP BY 1 ORDER BY 1"),
        ctx,
    )
    assert result["ok"] is True
    assert result["columns"] == ["workshop_id", "n"]
    assert len(result["rows"]) == 5


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM production_record",
        "DROP TABLE workshop",
        "SELECT 1; DROP TABLE workshop",
        "INSERT INTO workshop VALUES ('x')",
        "PRAGMA database_list",
    ],
)
async def test_run_sql_blocks_writes(ctx, statement):
    with pytest.raises(Exception) as exc:
        await sql.run_sql(sql.RunSqlArgs(sql=statement), ctx)
    assert "只读" in str(exc.value) or "禁止" in str(exc.value) or "一条语句" in str(exc.value)


async def test_run_sql_allows_cte(ctx):
    result = await sql.run_sql(
        sql.RunSqlArgs(
            sql="""
            WITH e AS (SELECT workshop_id, SUM(quantity) kwh FROM energy_record
                       WHERE energy_type='electricity' GROUP BY 1)
            SELECT COUNT(*) FROM e
            """
        ),
        ctx,
    )
    assert result["ok"] is True
    assert result["rows"][0][0] == 5


async def test_python_sandbox_runs_code_and_collects_artifact(ctx):
    code = """
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

print("sum:", 1 + 2)
plt.figure(figsize=(3, 2))
plt.plot([1, 2, 3], [3, 2, 1])
plt.savefig("demo.png", dpi=80)
"""
    result = await python_tool.python_analysis(python_tool.PythonAnalysisArgs(code=code), ctx)
    assert result["ok"] is True
    assert "sum: 3" in result["stdout"]
    assert any(p.endswith("demo.png") for p in result["artifacts"])


async def test_python_sandbox_reports_error_as_observation(ctx):
    result = await python_tool.python_analysis(
        python_tool.PythonAnalysisArgs(code="raise ValueError('boom')"), ctx
    )
    assert result["ok"] is False
    assert "boom" in (result["stderr"] + (result["error"] or ""))


async def test_python_sandbox_stdout_is_utf8(ctx):
    # 回归：-I 会忽略 PYTHONIOENCODING，曾导致中文输出乱码
    result = await python_tool.python_analysis(
        python_tool.PythonAnalysisArgs(code="print('单位电耗 1.323 kWh/件')"), ctx
    )
    assert result["ok"] is True
    assert "单位电耗" in result["stdout"]
    assert "1.323" in result["stdout"]


async def test_submit_report_stores_payload(ctx):
    args = report.SubmitReportArgs(
        title="测试报告",
        summary="这是一个足够长的摘要，包含数字 13.2% 与时间范围 2026-09-28。",
        findings=[
            report.Finding(statement="单位电耗上升", evidence="SQL 聚合结果", confidence="已验证事实")
        ],
        recommendations=["加装变频"],
        limitations=["未核实维修记录"],
    )
    result = await report.submit_report(args, ctx)
    assert result["accepted"] is True
    assert ctx.shared["report"]["title"] == "测试报告"


async def test_submit_report_rejects_short_summary(ctx):
    with pytest.raises(Exception) as exc:
        await report.submit_report(
            report.SubmitReportArgs(
                title="t",
                summary="太短",
                findings=[report.Finding(statement="s", evidence="e", confidence="待核实")],
            ),
            ctx,
        )
    assert "summary" in str(exc.value)


async def test_submit_report_fills_charts_from_artifacts(ctx):
    ctx.shared["artifacts"] = ["py_1/trend.png", "py_1/data.csv"]
    await report.submit_report(
        report.SubmitReportArgs(
            title="图表兜底",
            summary="这是一个足够长的摘要，包含数字 11.38% 与时间范围 2026-10。",
            findings=[report.Finding(statement="s", evidence="e", confidence="证据支持")],
        ),
        ctx,
    )
    # 只取图片，不含 csv
    assert ctx.shared["report"]["charts"] == ["py_1/trend.png"]


def test_sql_validator_classifies_permission_error():
    with pytest.raises(Exception) as exc:
        sql.validate_readonly_sql("DROP TABLE x")
    err = exc.value
    assert getattr(err, "kind", None) is ErrorKind.PERMISSION_DENIED


def test_api_args_validate_workshop_pattern():
    with pytest.raises(Exception):
        api.EquipmentQueryArgs(workshop_id="3号车间", start_date="2026-09-01", end_date="2026-09-10")
