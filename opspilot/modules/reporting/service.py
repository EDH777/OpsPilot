"""报告导出：结构化 JSON → Markdown。"""

from __future__ import annotations

import json
from typing import Any

CONFIDENCE_ICON = {"已验证事实": "✅", "证据支持": "◐", "待核实": "○"}


def dedupe_reports(rows: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """内容相同的报告只保留最新一份，并标注出现次数。

    背景：报告是"每轮 run 一份"的快照；同一段对话里重复问同一个问题、
    或离线演示反复跑同一剧本，会产生多份内容完全相同的报告。
    """
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        report = row.get("report") or {}
        # charts 里是每次运行各自的沙箱路径（py_xxx/xxx.png），不参与内容比较；
        # _meta 里有 run_id/generated_at，也不参与，否则永远无法去重。
        normalized = {k: v for k, v in report.items() if k not in {"charts", "_meta"}}
        key = json.dumps(
            {"owner": row.get("owner_user_id"), "content": normalized},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key not in grouped:
            grouped[key] = {**row, "duplicates": 1, "run_ids": [row.get("run_id")]}
            order.append(key)
        else:
            grouped[key]["duplicates"] += 1
            grouped[key]["run_ids"].append(row.get("run_id"))
    result = [grouped[key] for key in order]
    return result[:limit] if limit else result


def report_to_markdown(report: dict[str, Any], *, run_meta: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# {report.get('title') or '调查报告'}")
    lines.append("")

    meta = run_meta or {}
    period = report.get("period")
    if period:
        lines.append(f"- 统计区间：{period}")
    if meta.get("run_id"):
        lines.append(f"- 运行 ID：`{meta['run_id']}`")
    if meta.get("finished_at"):
        lines.append(f"- 生成时间：{meta['finished_at']}")
    if len(lines) > 1:
        lines.append("")

    lines.append("## 结论摘要")
    lines.append("")
    lines.append(report.get("summary") or "（无）")
    lines.append("")

    metrics = report.get("metrics") or []
    if metrics:
        lines.append("## 关键指标")
        lines.append("")
        lines.append("| 指标 | 数值 | 单位 | 统计区间 |")
        lines.append("|---|---|---|---|")
        for m in metrics:
            lines.append(f"| {m.get('name')} | {m.get('value')} | {m.get('unit') or '-'} | {m.get('period') or '-'} |")
        lines.append("")

    findings = report.get("findings") or []
    if findings:
        lines.append("## 结论与证据")
        lines.append("")
        for i, f in enumerate(findings, 1):
            icon = CONFIDENCE_ICON.get(f.get("confidence", ""), "•")
            lines.append(f"### {i}. {icon} {f.get('statement')}")
            lines.append("")
            lines.append(f"**把握程度**：{f.get('confidence')}")
            lines.append("")
            lines.append(f"**证据**：{f.get('evidence')}")
            lines.append("")

    if report.get("recommendations"):
        lines.append("## 改进建议")
        lines.append("")
        for r in report["recommendations"]:
            lines.append(f"- {r}")
        lines.append("")

    if report.get("limitations"):
        lines.append("## 局限与待核实项")
        lines.append("")
        for r in report["limitations"]:
            lines.append(f"- {r}")
        lines.append("")

    charts = report.get("charts") or []
    if charts:
        lines.append("## 图表")
        lines.append("")
        for c in charts:
            lines.append(f"- `{c}`")
        lines.append("")

    permission_meta = report.get("_meta") or {}
    if permission_meta:
        owner = permission_meta.get("owner") or {}
        scope = permission_meta.get("scope") or {}
        lines.append("---")
        lines.append("")
        lines.append("## 权限与数据范围")
        lines.append("")
        lines.append(
            f"- 生成者：{owner.get('name') or '-'}（{owner.get('role_label') or owner.get('role') or '-'}）"
        )
        lines.append(f"- 数据范围：{scope.get('label') or '-'}")
        lines.append(
            f"- 生成时间：{permission_meta.get('generated_at') or '-'}；"
            f"运行：`{permission_meta.get('run_id') or '-'}`"
        )
        lines.append(f"- {permission_meta.get('data_note') or '本报告仅基于上述范围内的数据。'}")
        lines.append("")

    usage = meta.get("usage") or {}
    if usage:
        lines.append("---")
        lines.append("")
        lines.append("## 附：执行指标")
        lines.append("")
        lines.append(
            f"- 推理步数 {meta.get('steps', '-')}，工具调用 {usage.get('llm_calls', '-')} 次模型请求，"
            f"token 合计 {usage.get('total_tokens', '-')}，耗时 {meta.get('duration_ms', '-')} ms"
        )
        lines.append("- 本报告由 OpsPilot 自主调查生成，所有数字均来自工具查询结果。")

    return "\n".join(lines) + "\n"
