"""对模拟数据做一次"标准答案"核对，供开发与验收使用。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb

from opspilot.config import get_settings


def main() -> None:
    con = duckdb.connect(str(get_settings().db_path), read_only=True)

    print("== 1) 单位电耗：异常窗口 vs 基线窗口 ==")
    print(
        con.execute(
            """
            WITH e AS (
                SELECT workshop_id, record_date, SUM(quantity) AS kwh
                FROM energy_record WHERE energy_type='electricity' GROUP BY 1,2
            ), p AS (
                SELECT workshop_id, record_date, SUM(output_qty) AS qty,
                       SUM(output_qty - defect_qty) AS good_qty
                FROM production_record GROUP BY 1,2
            )
            SELECT e.workshop_id,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN kwh/good_qty END),3) AS 基线,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN kwh/good_qty END),3) AS 异常期,
                   ROUND(100*(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN kwh/good_qty END)
                        / AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN kwh/good_qty END)-1),1) AS 变化百分比
            FROM e JOIN p USING (workshop_id, record_date)
            GROUP BY 1 ORDER BY 1
            """
        ).fetchdf().to_string(index=False)
    )

    print("\n== 2) 三号车间设备级证据（异常期 vs 基线）==")
    print(
        con.execute(
            """
            SELECT o.equipment_id, e.equipment_name,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN energy_kwh END),1) AS 基线日均电耗,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN energy_kwh END),1) AS 异常期日均电耗,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN idle_min END),0) AS 基线空转分钟,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN idle_min END),0) AS 异常期空转分钟,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-08-29' AND DATE '2026-09-27' THEN load_rate END),3) AS 基线负载率,
                   ROUND(AVG(CASE WHEN record_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-20' THEN load_rate END),3) AS 异常期负载率
            FROM equipment_operation o JOIN equipment e USING (equipment_id)
            WHERE o.workshop_id='WS-03'
            GROUP BY 1,2 ORDER BY 3 DESC
            """
        ).fetchdf().to_string(index=False)
    )

    print("\n== 3) 三号车间告警构成（异常期）==")
    print(
        con.execute(
            """
            SELECT a.alarm_code, c.name, COUNT(*) AS 次数
            FROM equipment_alarm a LEFT JOIN alarm_code c ON a.alarm_code = c.code
            WHERE a.workshop_id='WS-03' AND CAST(a.alarm_time AS DATE) BETWEEN DATE '2026-09-28' AND DATE '2026-10-20'
            GROUP BY 1,2 ORDER BY 3 DESC
            """
        ).fetchdf().to_string(index=False)
    )

    print("\n== 4) 五号车间干扰项：不良率 ==")
    print(
        con.execute(
            """
            SELECT ROUND(AVG(CASE WHEN record_date < DATE '2026-10-05' THEN 1.0*defect_qty/output_qty END)*100,2) AS 之前不良率,
                   ROUND(AVG(CASE WHEN record_date >= DATE '2026-10-05' THEN 1.0*defect_qty/output_qty END)*100,2) AS 之后不良率
            FROM production_record WHERE workshop_id='WS-05'
            """
        ).fetchdf().to_string(index=False)
    )

    print("\n== 5) 数据缺口：二号车间 10-10~10-12 电耗记录 ==")
    print(
        con.execute(
            """
            SELECT record_date, COUNT(*) AS 电耗记录数 FROM energy_record
            WHERE workshop_id='WS-02' AND energy_type='electricity'
              AND record_date BETWEEN DATE '2026-10-08' AND DATE '2026-10-14'
            GROUP BY 1 ORDER BY 1
            """
        ).fetchdf().to_string(index=False)
    )
    con.close()


if __name__ == "__main__":
    main()
