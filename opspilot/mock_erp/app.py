"""模拟企业业务系统（MES / 设备管理 / 生产系统）。

独立进程运行，端口 8101。故意保留真实企业系统常见的"参数口径不统一"：
- 日期必须 YYYY-MM-DD；
- 车间编号必须 WS-03；
- 月报只接受 YYYY-MM；
- 单次查询跨度不超过 92 天。

错误响应是结构化的（code / message / field / allowed / hint），
harness 会把它转成模型可读的 Observation，模型据此改参数重试。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import duckdb
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from opspilot.config import get_settings

app = FastAPI(title="OpsPilot Mock ERP", version="0.1.0")


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(get_settings().db_path), read_only=True)


def _err(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


def _parse_date(raw: str, field: str) -> date | JSONResponse:
    try:
        if len(raw) != 10:
            raise ValueError
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return _err(
            400,
            "INVALID_DATE",
            f"{field} 必须为 YYYY-MM-DD 格式，收到 {raw!r}",
            field=field,
            example="2026-09-01",
            hint="请把日期改成 2026-09-01 这种格式，不要带时间或使用斜杠。",
        )


def _check_workshop(con: duckdb.DuckDBPyConnection, workshop_id: str) -> JSONResponse | None:
    allowed = [r[0] for r in con.execute("SELECT workshop_id FROM workshop ORDER BY 1").fetchall()]
    if workshop_id not in allowed:
        return _err(
            400,
            "INVALID_WORKSHOP",
            f"车间编号 {workshop_id!r} 不存在，可用编号：{', '.join(allowed)}",
            field="workshop_id",
            allowed=allowed,
            hint=f"车间编号形如 WS-03；{workshop_id!r} 可能是车间名称而不是编号。",
        )
    return None


def _check_range(start: date, end: date) -> JSONResponse | None:
    if end < start:
        return _err(400, "INVALID_RANGE", "end_date 不能早于 start_date", field="end_date")
    if (end - start).days > 92:
        return _err(
            400,
            "RANGE_TOO_LARGE",
            f"查询跨度 {(end - start).days} 天，超过 92 天上限",
            field="end_date",
            hint="请缩小时间范围，或分多次查询后再汇总。",
        )
    return None


def _meta(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "query_id": f"q_{uuid.uuid4().hex[:10]}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "mock-erp",
        **extra,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "mock-erp", "version": "0.1.0"}


@app.get("/api/v1/workshops")
def workshops() -> dict[str, Any]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT workshop_id, workshop_name, process, line_count FROM workshop ORDER BY workshop_id"
        ).fetchall()
    finally:
        con.close()
    return {
        "items": [
            {"workshop_id": r[0], "workshop_name": r[1], "process": r[2], "line_count": r[3]} for r in rows
        ],
        "count": len(rows),
    }


@app.get("/api/v1/equipment/operations")
def equipment_operations(
    request: Request,
    workshop_id: str = Query(..., description="车间编号，如 WS-03"),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    equipment_type: str | None = Query(default=None),
) -> Any:
    start = _parse_date(start_date, "start_date")
    if isinstance(start, JSONResponse):
        return start
    end = _parse_date(end_date, "end_date")
    if isinstance(end, JSONResponse):
        return end
    if (range_err := _check_range(start, end)) is not None:
        return range_err

    con = _connect()
    try:
        if (ws_err := _check_workshop(con, workshop_id)) is not None:
            return ws_err
        sql = """
            SELECT o.record_date, o.equipment_id, e.equipment_name, e.equipment_type,
                   o.runtime_min, o.idle_min, o.load_rate, o.avg_temp_c, o.vibration_mm_s, o.energy_kwh
            FROM equipment_operation o JOIN equipment e USING (equipment_id)
            WHERE o.workshop_id = ? AND o.record_date BETWEEN ? AND ?
        """
        params: list[Any] = [workshop_id, start, end]
        if equipment_type:
            sql += " AND e.equipment_type = ?"
            params.append(equipment_type)
        sql += " ORDER BY o.record_date, o.equipment_id"
        rows = con.execute(sql, params).fetchall()
        items = [
            {
                "record_date": r[0].isoformat(),
                "equipment_id": r[1],
                "equipment_name": r[2],
                "equipment_type": r[3],
                "runtime_min": r[4],
                "idle_min": r[5],
                "load_rate": r[6],
                "avg_temp_c": r[7],
                "vibration_mm_s": r[8],
                "energy_kwh": r[9],
            }
            for r in rows
        ]
    finally:
        con.close()

    return {
        "data": items,
        "count": len(items),
        "meta": _meta(request, workshop_id=workshop_id, start_date=start.isoformat(), end_date=end.isoformat()),
    }


@app.get("/api/v1/equipment/alarms")
def equipment_alarms(
    request: Request,
    workshop_id: str = Query(...),
    start_date: str = Query(...),
    end_date: str = Query(...),
    severity: str | None = Query(default=None),
) -> Any:
    start = _parse_date(start_date, "start_date")
    if isinstance(start, JSONResponse):
        return start
    end = _parse_date(end_date, "end_date")
    if isinstance(end, JSONResponse):
        return end
    if (range_err := _check_range(start, end)) is not None:
        return range_err

    con = _connect()
    try:
        if (ws_err := _check_workshop(con, workshop_id)) is not None:
            return ws_err
        sql = """
            SELECT a.alarm_id, a.alarm_time, a.equipment_id, e.equipment_name, a.alarm_code, c.name,
                   a.severity, a.message, a.duration_min
            FROM equipment_alarm a
            LEFT JOIN equipment e USING (equipment_id)
            LEFT JOIN alarm_code c ON a.alarm_code = c.code
            WHERE a.workshop_id = ? AND CAST(a.alarm_time AS DATE) BETWEEN ? AND ?
        """
        params: list[Any] = [workshop_id, start, end]
        if severity:
            sql += " AND a.severity = ?"
            params.append(severity)
        sql += " ORDER BY a.alarm_time"
        rows = con.execute(sql, params).fetchall()
        items = [
            {
                "alarm_id": r[0],
                "alarm_time": r[1].isoformat(sep=" "),
                "equipment_id": r[2],
                "equipment_name": r[3],
                "alarm_code": r[4],
                "alarm_name": r[5],
                "severity": r[6],
                "message": r[7],
                "duration_min": r[8],
            }
            for r in rows
        ]
    finally:
        con.close()
    return {"data": items, "count": len(items), "meta": _meta(request, workshop_id=workshop_id)}


@app.get("/api/v1/production/summary")
def production_summary(
    request: Request,
    month: str = Query(..., description="YYYY-MM"),
    workshop_id: str | None = Query(default=None),
) -> Any:
    try:
        if len(month) != 7:
            raise ValueError
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        return _err(
            400,
            "INVALID_MONTH",
            f"month 必须为 YYYY-MM 格式，收到 {month!r}",
            field="month",
            example="2026-09",
            hint="月报接口只要年月，不要传具体日期。",
        )

    con = _connect()
    try:
        if workshop_id and (ws_err := _check_workshop(con, workshop_id)) is not None:
            return ws_err
        sql = """
            SELECT workshop_id,
                   SUM(output_qty) AS output_qty,
                   SUM(planned_qty) AS planned_qty,
                   SUM(defect_qty) AS defect_qty,
                   ROUND(SUM(output_qty) * 1.0 / NULLIF(SUM(planned_qty), 0), 4) AS completion_rate,
                   ROUND(SUM(defect_qty) * 1.0 / NULLIF(SUM(output_qty), 0), 4) AS defect_rate
            FROM production_record
            WHERE strftime(record_date, '%Y-%m') = ?
        """
        params: list[Any] = [month]
        if workshop_id:
            sql += " AND workshop_id = ?"
            params.append(workshop_id)
        sql += " GROUP BY 1 ORDER BY 1"
        rows = con.execute(sql, params).fetchall()
        items = [
            {
                "workshop_id": r[0],
                "output_qty": r[1],
                "planned_qty": r[2],
                "defect_qty": r[3],
                "completion_rate": r[4],
                "defect_rate": r[5],
            }
            for r in rows
        ]
    finally:
        con.close()
    return {"data": items, "count": len(items), "meta": _meta(request, month=month)}


@app.get("/api/v1/alarm-codes")
def alarm_codes() -> dict[str, Any]:
    con = _connect()
    try:
        rows = con.execute("SELECT code, name, description, typical_cause FROM alarm_code ORDER BY 1").fetchall()
    finally:
        con.close()
    return {
        "data": [
            {"code": r[0], "name": r[1], "description": r[2], "typical_cause": r[3]} for r in rows
        ]
    }


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host="127.0.0.1", port=8101, log_level="info")


if __name__ == "__main__":
    main()
