"""离线演示脚本：不依赖模型 API 就能跑通完整 Loop / Harness / 工具链。

脚本化的模型只是"决策"被固定，工具执行、参数校验、SQL、数据、报告落盘全是真实链路。
用于 CI、无网演示，以及验证 Harness 本身是否正常。
"""

from __future__ import annotations

from opspilot.llm.mock import ScriptedLLM, assistant, tool_call

SQL_UNIT_ENERGY = """
WITH e AS (
  SELECT workshop_id, record_date, SUM(quantity) AS kwh
  FROM energy_record WHERE energy_type='electricity' GROUP BY 1,2
), p AS (
  SELECT workshop_id, record_date, SUM(output_qty) AS qty, SUM(output_qty-defect_qty) AS good_qty
  FROM production_record GROUP BY 1,2
)
SELECT e.workshop_id,
       ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN kwh/good_qty END),3) AS base_unit,
       ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN kwh/good_qty END),3) AS anomaly_unit
FROM e JOIN p USING (workshop_id, record_date)
GROUP BY 1 ORDER BY 1
""".strip()

SQL_EQUIPMENT = """
SELECT o.equipment_id, e.equipment_name,
       ROUND(AVG(CASE WHEN record_date < DATE '2026-09-28' THEN idle_min END),0) AS base_idle,
       ROUND(AVG(CASE WHEN record_date >= DATE '2026-09-28' THEN idle_min END),0) AS anomaly_idle,
       ROUND(AVG(CASE WHEN record_date < DATE '2026-09-28' THEN load_rate END),3) AS base_load,
       ROUND(AVG(CASE WHEN record_date >= DATE '2026-09-28' THEN load_rate END),3) AS anomaly_load,
       ROUND(AVG(CASE WHEN record_date < DATE '2026-09-28' THEN energy_kwh END),1) AS base_energy,
       ROUND(AVG(CASE WHEN record_date >= DATE '2026-09-28' THEN energy_kwh END),1) AS anomaly_energy
FROM equipment_operation o JOIN equipment e USING (equipment_id)
WHERE o.workshop_id='WS-03' AND o.record_date >= DATE '2026-08-29'
GROUP BY 1,2 ORDER BY 7 DESC
""".strip()

SQL_ALARMS = """
SELECT alarm_code, COUNT(*) AS cnt
FROM equipment_alarm
WHERE workshop_id='WS-03' AND CAST(alarm_time AS DATE) BETWEEN DATE '2026-09-28' AND DATE '2026-10-20'
GROUP BY 1 ORDER BY 2 DESC
""".strip()

CODE_CHART = """
import os
import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

con = duckdb.connect(os.environ["OPSPILOT_DB_PATH"], read_only=True)
df = con.execute('''
    WITH e AS (
        SELECT record_date, SUM(quantity) AS kwh FROM energy_record
        WHERE workshop_id = 'WS-03' AND energy_type = 'electricity' GROUP BY 1
    ), p AS (
        SELECT record_date, SUM(output_qty - defect_qty) AS good FROM production_record
        WHERE workshop_id = 'WS-03' GROUP BY 1
    )
    SELECT e.record_date, e.kwh / NULLIF(p.good, 0) AS unit_kwh
    FROM e JOIN p USING (record_date) ORDER BY 1
''').fetchdf()
con.close()

plt.figure(figsize=(8.4, 3.2))
plt.plot(df["record_date"], df["unit_kwh"], color="#ea580c", linewidth=1.8)
plt.title("WS-03 unit energy (kWh/piece)", fontsize=10)
plt.grid(alpha=0.25)
plt.tight_layout()
plt.savefig("unit_energy_trend.png", dpi=140)
print("chart saved, points:", len(df))
""".strip()


def mock_investigation() -> ScriptedLLM:
    report = {
        "title": "三号车间单位产品电耗上升原因调查",
        "summary": (
            "近30天三号车间单位产品电耗由约 2.82 kWh/件升至 3.19 kWh/件（+13.2%），"
            "同期产量基本持平（约1210件/日）。证据指向空压机1号与风机1号长期低负载空转："
            "空压机日空转时长由176分钟升至500分钟、负载率由0.70降至0.45，日耗电由638kWh升至998kWh。"
        ),
        "metrics": [
            {"name": "单位产品电耗", "value": 3.191, "unit": "kWh/件", "period": "2026-09-28~2026-10-20"},
            {"name": "空压机日空转时长", "value": 500, "unit": "分钟", "period": "异常期均值"},
        ],
        "findings": [
            {
                "statement": "三号车间单位产品电耗由 2.82 升至 3.19 kWh/件，上升 13.2%，同期日均产量基本持平",
                "evidence": "energy_record 与 production_record 按日关联聚合：2026-08-29~09-27 vs 2026-09-28~10-20",
                "confidence": "已验证事实",
            },
            {
                "statement": "上升主要来自空压机1号与风机1号的空转耗电，空压机日耗电增加约360kWh",
                "evidence": "equipment_operation 按设备聚合：AC01 空转176→500分钟、负载率0.70→0.45、电耗638→998kWh",
                "confidence": "证据支持",
            },
            {
                "statement": "同期设备告警以 E-207 低负载空转为主（41次），与空转时长上升时间吻合",
                "evidence": "equipment_alarm 按告警码统计；E-207 定义见 alarm_code 表",
                "confidence": "证据支持",
            },
        ],
        "recommendations": [
            "为空压机加装变频或群控联锁，按用气量自动加卸载，避免长期空载运行",
            "排查压缩空气管网泄漏点，减少为维持压力而产生的空转",
            "将空转率纳入设备日常看板，设置阈值告警并纳入班组考核",
        ],
        "limitations": [
            "未核实空压机加卸载控制参数与管网泄漏检测记录，无法确认空转的具体触发原因",
            "未取得设备维修工单，无法排除设备老化导致的效率下降",
        ],
        "charts": [],
    }
    return ScriptedLLM(
        [
            assistant(tool_calls=[tool_call("inspect_schema", {})]),
            assistant(tool_calls=[tool_call("run_sql", {"sql": SQL_UNIT_ENERGY, "purpose": "对比各车间单位电耗基线期与异常期"})]),
            assistant(tool_calls=[tool_call("run_sql", {"sql": SQL_EQUIPMENT, "purpose": "定位三号车间设备级空转与电耗变化"})]),
            assistant(tool_calls=[tool_call("run_sql", {"sql": SQL_ALARMS, "purpose": "核对异常期告警构成"})]),
            assistant(tool_calls=[tool_call("python_analysis", {"code": CODE_CHART, "purpose": "绘制三号车间单位电耗趋势"})]),
            assistant(
                content="证据已足够：单位电耗上升由空压机与风机长期低负载空转导致。现在提交报告。",
                tool_calls=[tool_call("submit_report", report)],
            ),
        ]
    )


def mock_error_recovery() -> ScriptedLLM:
    """现场演示"工具报错 → 分类 → 修正 → 重试成功"。

    第 2 步故意请求 143 天，模拟业务系统返回 400 RANGE_TOO_LARGE；
    第 3 步把窗口缩到 81 天后成功。整个链路（HTTP、400 解析、错误分类、回填、
    再次调用）都是真实的，只有"模型决策"是脚本化的。
    """
    report = {
        "title": "设备 API 错误恢复演示报告",
        "summary": (
            "设备接口对单次查询跨度限制为 92 天。首次以 113 天窗口请求被 400 拒绝，"
            "Harness 将其归类为 invalid_argument 并回填错误详情，"
            "第二次把窗口缩小到 81 天后成功返回 486 条设备运行记录。"
        ),
        "metrics": [{"name": "返回记录数", "value": 486, "unit": "条", "period": "2026-08-01~2026-10-20"}],
        "findings": [
            {
                "statement": "接口跨度超限时，Agent 能读取 400 详情并自动缩小时间窗口后重试成功",
                "evidence": "第一次 query_equipment_api 返回 RANGE_TOO_LARGE（HTTP 400）；第二次返回 200 且 count=486",
                "confidence": "已验证事实",
            }
        ],
        "recommendations": ["在工具侧预校验时间跨度，可减少一次往返"],
        "limitations": ["本演示使用脚本化决策，真实模型的行为可能略有差异"],
        "charts": [],
    }
    return ScriptedLLM(
        [
            assistant(tool_calls=[tool_call("inspect_schema", {"table": "equipment_operation"})]),
            assistant(
                content="先按车间和时间范围取设备运行明细（这次故意把窗口拉大，观察 Harness 如何处理 400）。",
                tool_calls=[
                    tool_call(
                        "query_equipment_api",
                        {"workshop_id": "WS-03", "start_date": "2026-06-29", "end_date": "2026-10-20"},
                    )
                ],
            ),
            assistant(
                content="接口提示跨度超过 92 天，缩小到 81 天窗口重试。",
                tool_calls=[
                    tool_call(
                        "query_equipment_api",
                        {"workshop_id": "WS-03", "start_date": "2026-08-01", "end_date": "2026-10-20"},
                    )
                ],
            ),
            assistant(tool_calls=[tool_call("submit_report", report)]),
        ]
    )
