"""报告去重与导出测试。"""

from __future__ import annotations

from opspilot.modules.reporting.service import dedupe_reports, report_to_markdown


def test_dedupe_reports_groups_identical_content():
    rows = [
        {"run_id": "a", "created_at": 3, "report": {"title": "T", "summary": "S"}},
        {"run_id": "b", "created_at": 2, "report": {"title": "T", "summary": "S"}},
        {"run_id": "c", "created_at": 1, "report": {"title": "T2", "summary": "S2"}},
    ]
    out = dedupe_reports(rows)
    assert len(out) == 2
    assert out[0]["run_id"] == "a"  # 保留最新一份
    assert out[0]["duplicates"] == 2
    assert out[0]["run_ids"] == ["a", "b"]
    assert out[1]["duplicates"] == 1


def test_dedupe_reports_limit():
    rows = [{"run_id": str(i), "report": {"title": f"T{i}", "summary": "S"}} for i in range(5)]
    assert len(dedupe_reports(rows, limit=3)) == 3


def test_dedupe_reports_distinguishes_full_content():
    # 标题/摘要相同但指标不同，不能当重复
    rows = [
        {"run_id": "a", "report": {"title": "T", "summary": "S"}},
        {"run_id": "b", "report": {"title": "T", "summary": "S", "metrics": [{"name": "x", "value": 1}]}},
    ]
    assert len(dedupe_reports(rows)) == 2


def test_dedupe_reports_ignores_chart_paths():
    # 图表路径每次运行都不同（py_xxx/...），不应影响去重
    rows = [
        {"run_id": "a", "report": {"title": "T", "summary": "S", "charts": ["py_1/trend.png"]}},
        {"run_id": "b", "report": {"title": "T", "summary": "S", "charts": ["py_2/trend.png"]}},
    ]
    out = dedupe_reports(rows)
    assert len(out) == 1
    assert out[0]["duplicates"] == 2


def test_report_markdown_contains_evidence():
    md = report_to_markdown(
        {
            "title": "测试报告",
            "summary": "单位电耗上升 13.2%",
            "metrics": [{"name": "单位电耗", "value": 3.19, "unit": "kWh/件", "period": "2026-10"}],
            "findings": [
                {
                    "statement": "空压机空转导致电耗上升",
                    "evidence": "equipment_operation 聚合",
                    "confidence": "证据支持",
                }
            ],
            "recommendations": ["加装变频"],
            "limitations": ["未核实维修记录"],
        },
        run_meta={"run_id": "run_x", "steps": 6},
    )
    assert "# 测试报告" in md
    assert "证据支持" in md
    assert "加装变频" in md
    assert "13.2%" in md
