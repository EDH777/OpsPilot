"""驾驶舱分析：把数据库里的原始记录聚合成 UI 需要的 KPI / 趋势 / 异常排行。

注意：这里的指标口径与 modules.metrics.definitions 保持一致（单位产品电耗 = 电力 / 合格产量），
驾驶舱和 Agent 使用同一套定义，避免"页面一个数、报告一个数"。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from opspilot.tools._common import connect_readonly


def _window(con, days: int) -> tuple[date, date, date, date]:
    end = con.execute("SELECT MAX(record_date) FROM production_record").fetchone()[0]
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return start, end, prev_start, prev_end


def _num(value: Any, digits: int = 2) -> float:
    return round(float(value), digits) if value is not None else 0.0


def dashboard_overview(
    db_path: Path, days: int = 30, workshop_id: str | None = None
) -> dict[str, Any]:
    con = connect_readonly(db_path)
    try:
        start, end, prev_start, prev_end = _window(con, days)
        ws_sql = " AND workshop_id = ?" if workshop_id else ""
        ws_params: list[Any] = [workshop_id] if workshop_id else []

        def kpi(sql: str, a: date, b: date) -> float:
            row = con.execute(sql + ws_sql, [a, b, *ws_params]).fetchone()
            return _num(row[0]) if row else 0.0

        output_sql = "SELECT SUM(output_qty) FROM production_record WHERE record_date BETWEEN ? AND ?"
        planned_sql = "SELECT SUM(planned_qty) FROM production_record WHERE record_date BETWEEN ? AND ?"
        defect_sql = "SELECT SUM(defect_qty) FROM production_record WHERE record_date BETWEEN ? AND ?"
        energy_sql = (
            "SELECT SUM(quantity) FROM energy_record "
            "WHERE energy_type='electricity' AND record_date BETWEEN ? AND ?"
        )
        alarm_sql = (
            "SELECT COUNT(*) FROM equipment_alarm WHERE CAST(alarm_time AS DATE) BETWEEN ? AND ?"
        )

        output = kpi(output_sql, start, end)
        planned = kpi(planned_sql, start, end)
        defects = kpi(defect_sql, start, end)
        energy = kpi(energy_sql, start, end)
        alarms = kpi(alarm_sql, start, end)

        prev_output = kpi(output_sql, prev_start, prev_end)
        prev_energy = kpi(energy_sql, prev_start, prev_end)
        prev_alarms = kpi(alarm_sql, prev_start, prev_end)

        good = max(output - defects, 1)
        prev_good = max(prev_output - kpi(defect_sql, prev_start, prev_end), 1)
        unit_energy = energy / good
        prev_unit_energy = prev_energy / prev_good

        kpis = [
            _kpi("output", "总产量", output, "件", _delta(output, prev_output), decimals=0),
            _kpi("completion", "计划完成率", output / planned * 100 if planned else 0, "%", None, decimals=1),
            _kpi("energy", "总用电量", energy, "kWh", _delta(energy, prev_energy), decimals=0, inverse=True),
            _kpi(
                "unit_energy",
                "综合单位电耗",
                unit_energy,
                "kWh/件",
                _delta(unit_energy, prev_unit_energy),
                decimals=3,
                highlight=True,
                inverse=True,
            ),
            _kpi("alarms", "设备告警", alarms, "次", _delta(alarms, prev_alarms), decimals=0, inverse=True),
        ]

        # ---- 趋势：各车间每日单位电耗 ----
        trend_ws_e = " AND workshop_id = ?" if workshop_id else ""
        trend_ws_p = " AND workshop_id = ?" if workshop_id else ""
        trend_params: list[Any] = []
        if workshop_id:
            trend_params.append(workshop_id)
        if workshop_id:
            trend_params.append(workshop_id)
        trend_params.extend([start, end])
        trend_rows = con.execute(
            f"""
            WITH e AS (
                SELECT workshop_id, record_date, SUM(quantity) AS kwh
                FROM energy_record WHERE energy_type='electricity'{trend_ws_e} GROUP BY 1,2
            ), p AS (
                SELECT workshop_id, record_date, SUM(output_qty-defect_qty) AS good_qty
                FROM production_record WHERE 1=1{trend_ws_p} GROUP BY 1,2
            )
            SELECT e.record_date, e.workshop_id, e.kwh / NULLIF(p.good_qty, 0) AS unit
            FROM e JOIN p USING (workshop_id, record_date)
            WHERE e.record_date BETWEEN ? AND ?
            ORDER BY 1, 2
            """,
            trend_params,
        ).fetchall()

        dates = sorted({r[0].isoformat() for r in trend_rows})
        ws_rows = con.execute(
            "SELECT workshop_id, workshop_name FROM workshop"
            + (" WHERE workshop_id = ?" if workshop_id else "")
            + " ORDER BY 1",
            [workshop_id] if workshop_id else [],
        ).fetchall()
        name_map = {r[0]: r[1] for r in ws_rows}
        by_ws: dict[str, dict[str, float]] = {ws: {} for ws in name_map}
        for d, ws, unit in trend_rows:
            if unit is not None:
                by_ws.setdefault(ws, {})[d.isoformat()] = round(float(unit), 3)
        series = [
            {
                "name": name_map.get(ws, ws),
                "workshop_id": ws,
                "data": [by_ws.get(ws, {}).get(d) for d in dates],
            }
            for ws in sorted(by_ws)
        ]

        # ---- 车间对比：本期 vs 上期单位电耗（按"有完整数据的日期"配对计算）----
        compare_current = _unit_energy_by_workshop(con, start, end, workshop_id)
        compare_prev = _unit_energy_by_workshop(con, prev_start, prev_end, workshop_id)
        expected_days = (end - start).days + 1
        workshops = [name_map[ws] for ws in sorted(name_map)]
        current_vals = [compare_current.get(ws, {}).get("unit") for ws in sorted(name_map)]
        prev_vals = [compare_prev.get(ws, {}).get("unit") for ws in sorted(name_map)]

        anomalies = []
        data_quality = []
        for ws, name in name_map.items():
            cur_info = compare_current.get(ws) or {}
            prev_info = compare_prev.get(ws) or {}
            coverage = cur_info.get("matched_days", 0) / expected_days
            if coverage < 0.95:
                data_quality.append(
                    {
                        "workshop_id": ws,
                        "workshop_name": name,
                        "issue": f"本期仅 {cur_info.get('matched_days', 0)}/{expected_days} 天有完整电耗+产量记录",
                        "hint": "缺失日期不参与单位电耗计算，请勿据此判断趋势",
                    }
                )
                continue
            cur, prev = cur_info.get("unit"), prev_info.get("unit")
            if cur and prev:
                change = (cur / prev - 1) * 100
                if abs(change) >= 3:
                    anomalies.append(
                        {
                            "workshop_id": ws,
                            "workshop_name": name,
                            "metric": "单位产品电耗",
                            "current": round(cur, 3),
                            "previous": round(prev, 3),
                            "change_pct": round(change, 1),
                            "unit": "kWh/件",
                        }
                    )
        anomalies.sort(key=lambda x: x["change_pct"], reverse=True)

        alarm_rows = con.execute(
            """
            SELECT a.workshop_id, w.workshop_name, COUNT(*) AS cnt
            FROM equipment_alarm a JOIN workshop w USING (workshop_id)
            WHERE CAST(a.alarm_time AS DATE) BETWEEN ? AND ?
            """
            + (" AND a.workshop_id = ?" if workshop_id else "")
            + " GROUP BY 1, 2 ORDER BY 3 DESC",
            [start, end, *([workshop_id] if workshop_id else [])],
        ).fetchall()

        return {
            "period": {
                "days": days,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "previous_start": prev_start.isoformat(),
                "previous_end": prev_end.isoformat(),
            },
            "kpis": kpis,
            "unit_energy_trend": {"dates": dates, "series": series},
            "workshop_compare": {
                "workshops": workshops,
                "current": current_vals,
                "previous": prev_vals,
                "unit": "kWh/件",
            },
            "anomalies": anomalies,
            "data_quality": data_quality,
            "alarms_by_workshop": [
                {"workshop_id": r[0], "workshop_name": r[1], "count": r[2]} for r in alarm_rows
            ],
            "data_sources": [
                {"name": "生产数据仓库 (DuckDB)", "type": "SQL", "status": "connected"},
                {"name": "设备管理系统 (Mock ERP)", "type": "HTTP API", "status": "connected"},
                {"name": "生产管理系统 (Mock ERP)", "type": "HTTP API", "status": "connected"},
            ],
        }
    finally:
        con.close()


def _unit_energy_by_workshop(
    con, start: date, end: date, workshop_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """只统计电耗与产量都有记录的日期，避免缺失数据把指标拉偏。"""
    ws_e = " AND workshop_id = ?" if workshop_id else ""
    ws_p = " AND workshop_id = ?" if workshop_id else ""
    ws_params = [workshop_id] if workshop_id else []
    rows = con.execute(
        f"""
        WITH e AS (
            SELECT workshop_id, record_date, SUM(quantity) AS kwh
            FROM energy_record
            WHERE energy_type='electricity' AND record_date BETWEEN ? AND ?{ws_e}
            GROUP BY 1, 2
        ), p AS (
            SELECT workshop_id, record_date, SUM(output_qty-defect_qty) AS good_qty
            FROM production_record
            WHERE record_date BETWEEN ? AND ?{ws_p}
            GROUP BY 1, 2
        )
        SELECT e.workshop_id, SUM(e.kwh) / NULLIF(SUM(p.good_qty), 0) AS unit, COUNT(*) AS matched_days
        FROM e JOIN p USING (workshop_id, record_date)
        GROUP BY 1
        """,
        [start, end, *ws_params, start, end, *ws_params],
    ).fetchall()
    return {
        r[0]: {"unit": round(float(r[1]), 3) if r[1] is not None else None, "matched_days": r[2]}
        for r in rows
    }


def _delta(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round((current / previous - 1) * 100, 1)


def _kpi(
    key: str,
    label: str,
    value: float,
    unit: str,
    delta: float | None,
    *,
    decimals: int = 2,
    highlight: bool = False,
    inverse: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": round(float(value), decimals),
        "unit": unit,
        "delta_pct": delta,
        "highlight": highlight,
        "inverse": inverse,
    }
