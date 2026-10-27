"""生成模拟制造企业数据库（DuckDB）。

设计目标不是"造点随机数"，而是让 Agent 真的能查出一条可复核的证据链：

三号车间（WS-03）从 2026-09-28 起，空压机 EQ-WS03-AC01 与风机 EQ-WS03-FAN01
长期低负载空转（空转时长翻倍、负载率下降），设备用电上升，而产量持平，
最终表现为"单位产品电耗上升 ~12%"。

同时埋入：
- 干扰项：五号车间 10 月起不良率上升（与能耗无关，防止 Agent 乱归因）；
- 数据缺口：二号车间 2026-10-10~12 的电耗记录缺失（考验 Agent 是否诚实说明）；
- 基线告警噪声：温度偏高、振动超标等日常告警。

数据来源说明：本库为**合成数据**，仅用于演示调查流程，不代表任何真实工厂。
公开数据集（UCI Steel Energy / AI4I）可在 data/raw/ 就绪时另行导入，二者不可拼接后
宣称发现真实跨系统因果。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

SEED = 20261027
END_DATE = date(2026, 10, 26)
DAYS = 120
START_DATE = END_DATE - timedelta(days=DAYS - 1)
ANOMALY_START = date(2026, 9, 28)
ANOMALY_END = date(2026, 10, 20)
QUALITY_ANOMALY_START = date(2026, 10, 5)
MISSING_ENERGY_DAYS = {date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)}

SCHEMA = """
CREATE TABLE workshop (
    workshop_id   VARCHAR PRIMARY KEY,
    workshop_name VARCHAR,
    process       VARCHAR,
    area_m2       INTEGER,
    line_count    INTEGER,
    manager       VARCHAR
);

CREATE TABLE production_record (
    record_date  DATE,
    workshop_id  VARCHAR,
    shift        VARCHAR,
    product_code VARCHAR,
    output_qty   INTEGER,
    planned_qty  INTEGER,
    defect_qty   INTEGER
);

CREATE TABLE energy_record (
    record_date DATE,
    workshop_id VARCHAR,
    energy_type VARCHAR,
    quantity    DOUBLE,
    unit        VARCHAR,
    cost_cny    DOUBLE
);

CREATE TABLE equipment (
    equipment_id    VARCHAR PRIMARY KEY,
    equipment_name  VARCHAR,
    equipment_type  VARCHAR,
    workshop_id     VARCHAR,
    rated_power_kw  DOUBLE,
    commissioned_on DATE
);

CREATE TABLE equipment_operation (
    record_date    DATE,
    equipment_id   VARCHAR,
    workshop_id    VARCHAR,
    runtime_min    DOUBLE,
    idle_min       DOUBLE,
    load_rate      DOUBLE,
    avg_temp_c     DOUBLE,
    vibration_mm_s DOUBLE,
    energy_kwh     DOUBLE
);

CREATE TABLE equipment_alarm (
    alarm_id    VARCHAR,
    alarm_time  TIMESTAMP,
    equipment_id VARCHAR,
    workshop_id VARCHAR,
    alarm_code  VARCHAR,
    severity    VARCHAR,
    message     VARCHAR,
    duration_min DOUBLE
);

CREATE TABLE alarm_code (
    code          VARCHAR PRIMARY KEY,
    name          VARCHAR,
    description   VARCHAR,
    typical_cause VARCHAR
);
"""


@dataclass
class Workshop:
    workshop_id: str
    workshop_name: str
    process: str
    area_m2: int
    line_count: int
    manager: str
    base_output: int
    defect_base: float


WORKSHOPS = [
    Workshop("WS-01", "一号车间", "冲压", 4200, 3, "李强", 1500, 0.011),
    Workshop("WS-02", "二号车间", "焊装", 3800, 2, "王敏", 1100, 0.013),
    Workshop("WS-03", "三号车间", "注塑", 3500, 3, "赵磊", 1200, 0.012),
    Workshop("WS-04", "四号车间", "总装", 5000, 2, "陈静", 900, 0.010),
    Workshop("WS-05", "五号车间", "涂装", 3000, 2, "孙浩", 800, 0.018),
]

# (类型前缀, 中文名, 额定功率kW, 日运行基数min, 日空转基数min)
TEMPLATES: dict[str, list[tuple[str, str, float, float, float]]] = {
    "冲压": [("PRESS", "压机", 120, 780, 140), ("CONV", "输送线", 12, 1300, 80)],
    "焊装": [("WELD", "焊机", 60, 900, 120), ("FAN", "风机", 30, 850, 150), ("CONV", "输送线", 12, 1200, 90)],
    "注塑": [
        ("IM", "注塑机", 45, 1100, 180),
        ("IM", "注塑机", 45, 1050, 200),
        ("AC", "空压机", 75, 420, 180),
        ("FAN", "风机", 30, 900, 150),
        ("CT", "冷却塔", 22, 650, 90),
        ("CONV", "输送线", 12, 1300, 90),
    ],
    "总装": [("CONV", "输送线", 12, 1250, 80), ("TORQUE", "拧紧机", 18, 700, 120), ("FAN", "风机", 22, 700, 120)],
    "涂装": [("OVEN", "烘干炉", 90, 1000, 100), ("FAN", "风机", 45, 950, 130), ("AC", "空压机", 55, 380, 160)],
}

PRODUCTS = {
    "WS-01": "P-STAMP-01",
    "WS-02": "P-WELD-01",
    "WS-03": "P-MOLD-01",
    "WS-04": "P-ASSY-01",
    "WS-05": "P-PAINT-01",
}

ALARM_CODES = [
    ("E-101", "温度偏高", "设备运行温度超过阈值", "散热不良或负载长期偏高"),
    ("E-113", "管网压力波动", "压缩空气管网压力波动超出允许区间", "管网泄漏或空压机加卸载频繁"),
    ("E-207", "低负载空转", "设备通电运行但长时间无有效负载", "产能不饱和、由定频设备维持压力/风量"),
    ("E-210", "振动超标", "轴承或传动部件振动超阈值", "机械磨损或对中不良"),
    ("E-305", "润滑周期到", "达到计划润滑保养周期", "按计划执行保养"),
    ("E-402", "通讯中断", "设备网关与采集系统短时失联", "网络抖动"),
]

BASELINE_ALARMS = ["E-101", "E-210", "E-305", "E-402"]
ANOMALY_EQUIPMENT = {"EQ-WS03-AC01", "EQ-WS03-FAN01"}


def _jitter(rng: random.Random, scale: float = 0.03) -> float:
    return 1.0 + rng.uniform(-scale, scale)


def _weekday_factor(d: date) -> float:
    return 0.86 if d.weekday() >= 5 else 1.0


def _season_temp(d: date) -> float:
    # 夏季偏热，用于空调/冷却负荷
    return 22.0 + 9.0 * math.sin(2 * math.pi * (d.timetuple().tm_yday - 100) / 365)


def build(force: bool = True) -> Path:
    from opspilot.config import get_settings

    db_path = get_settings().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and force:
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA)

    rng = random.Random(SEED)
    equipment_rows: list[tuple] = []
    equipment_meta: dict[str, tuple[Workshop, str, float, float, float]] = {}

    for ws in WORKSHOPS:
        con.execute(
            "INSERT INTO workshop VALUES (?, ?, ?, ?, ?, ?)",
            (ws.workshop_id, ws.workshop_name, ws.process, ws.area_m2, ws.line_count, ws.manager),
        )
        counters: dict[str, int] = {}
        for prefix, cn, power, runtime, idle in TEMPLATES[ws.process]:
            counters[prefix] = counters.get(prefix, 0) + 1
            eq_id = f"EQ-{ws.workshop_id.replace('-', '')}-{prefix}{counters[prefix]:02d}"
            name = f"{cn}{counters[prefix]}号"
            commissioned = date(2019, 1, 1) + timedelta(days=rng.randint(0, 2000))
            equipment_rows.append((eq_id, name, cn, ws.workshop_id, power, commissioned))
            equipment_meta[eq_id] = (ws, cn, power, runtime, idle)

    con.executemany("INSERT INTO equipment VALUES (?, ?, ?, ?, ?, ?)", equipment_rows)
    con.executemany("INSERT INTO alarm_code VALUES (?, ?, ?, ?)", ALARM_CODES)

    production_rows: list[tuple] = []
    energy_rows: list[tuple] = []
    operation_rows: list[tuple] = []
    alarm_rows: list[tuple] = []
    alarm_seq = 0

    day = START_DATE
    day_index = 0
    while day <= END_DATE:
        temp = _season_temp(day) + rng.uniform(-3, 3)
        weekend = _weekday_factor(day)

        for ws in WORKSHOPS:
            # ---- 设备运行（先算设备用电，车间电表由设备汇总而来，保证可交叉验证）----
            equip_energy = 0.0
            for eq_id, (owner, cn, power, base_runtime, base_idle) in equipment_meta.items():
                if owner.workshop_id != ws.workshop_id:
                    continue

                runtime = base_runtime * weekend * _jitter(rng, 0.05)
                idle = base_idle * _jitter(rng, 0.10)

                in_anomaly = ANOMALY_START <= day <= ANOMALY_END
                if in_anomaly and eq_id in ANOMALY_EQUIPMENT:
                    # 低负载空转：运行时长不变，空转时长翻倍，负载率显著下降
                    multiplier = 2.8 if cn == "空压机" else 2.2
                    idle = base_idle * multiplier * _jitter(rng, 0.08)
                    runtime = base_runtime * weekend * _jitter(rng, 0.04)

                total_on = runtime + idle
                load_rate = runtime / total_on if total_on else 0.0
                # 定频设备（空压机/风机/冷却塔）空转时仍接近满功率
                power_factor = 0.88 if cn in {"空压机", "风机", "冷却塔"} else load_rate
                energy = power * total_on / 60.0 * power_factor

                vibration = 2.1 + rng.uniform(-0.5, 1.4) + (0.9 if (in_anomaly and eq_id in ANOMALY_EQUIPMENT) else 0)
                operation_rows.append(
                    (
                        day,
                        eq_id,
                        ws.workshop_id,
                        round(runtime, 1),
                        round(idle, 1),
                        round(load_rate, 4),
                        round(temp + (6 if cn in {"空压机", "注塑机", "烘干炉"} else 2) + rng.uniform(-1.5, 1.5), 1),
                        round(vibration, 2),
                        round(energy, 1),
                    )
                )
                equip_energy += energy

            # 电表 = 设备用电 × 厂务公摊(约8%)
            electricity = equip_energy * 1.08 * _jitter(rng, 0.02)
            if day not in MISSING_ENERGY_DAYS or ws.workshop_id != "WS-02":
                energy_rows.append((day, ws.workshop_id, "electricity", round(electricity, 1), "kWh", round(electricity * 0.68, 2)))
            energy_rows.append((day, ws.workshop_id, "water", round(18 + ws.area_m2 / 1000 * 2 + rng.uniform(-2, 2), 1), "t", 0.0))
            if ws.workshop_id == "WS-05":
                gas = 900 + 0.45 * ws.base_output + rng.uniform(-40, 40)
                energy_rows.append((day, ws.workshop_id, "natural_gas", round(gas, 1), "m3", round(gas * 2.9, 2)))

            # ---- 产量（两班制；Agent 必须按日期+车间 SUM 后才能和能耗对齐）----
            defect_rate = ws.defect_base
            if ws.workshop_id == "WS-05" and day >= QUALITY_ANOMALY_START:
                defect_rate = 0.045

            growth = 1 + 0.0006 * day_index
            for shift, ratio in (("白班", 0.58), ("夜班", 0.42)):
                output = ws.base_output * ratio * weekend * growth * _jitter(rng, 0.04)
                planned = ws.base_output * ratio * 1.04
                production_rows.append(
                    (
                        day,
                        ws.workshop_id,
                        shift,
                        PRODUCTS[ws.workshop_id],
                        int(round(output)),
                        int(round(planned)),
                        int(round(output * defect_rate)),
                    )
                )

            # ---- 告警 ----
            workshop_equipment = [e for e, meta in equipment_meta.items() if meta[0].workshop_id == ws.workshop_id]

            if ANOMALY_START <= day <= ANOMALY_END:
                for eq_id in ANOMALY_EQUIPMENT:
                    if equipment_meta[eq_id][0].workshop_id != ws.workshop_id:
                        continue
                    if rng.random() < 0.85:
                        alarm_seq += 1
                        code, name, _desc, _cause = ALARM_CODES[2]
                        eq_name = next(r[1] for r in equipment_rows if r[0] == eq_id)
                        alarm_rows.append(
                            (
                                f"ALM-{alarm_seq:06d}",
                                datetime.combine(day, datetime.min.time()).replace(hour=rng.randint(7, 20), minute=rng.randint(0, 59)),
                                eq_id,
                                ws.workshop_id,
                                code,
                                "中",
                                f"{eq_name} 低负载空转，连续空转 {rng.randint(90, 300)} 分钟",
                                float(rng.randint(60, 300)),
                            )
                        )
                    if (day - ANOMALY_START).days % 3 == 0 and rng.random() < 0.5:
                        alarm_seq += 1
                        alarm_rows.append(
                            (
                                f"ALM-{alarm_seq:06d}",
                                datetime.combine(day, datetime.min.time()).replace(hour=rng.randint(8, 18), minute=rng.randint(0, 59)),
                                eq_id,
                                ws.workshop_id,
                                "E-113",
                                "中",
                                "压缩空气管网压力波动超过 0.15MPa",
                                float(rng.randint(15, 90)),
                            )
                        )

            if rng.random() < 0.22:
                alarm_seq += 1
                eq_id = rng.choice(workshop_equipment)
                code = rng.choice(BASELINE_ALARMS)
                sev = "低" if code in {"E-305", "E-402"} else "中"
                name = next(r[1] for r in equipment_rows if r[0] == eq_id)
                alarm_rows.append(
                    (
                        f"ALM-{alarm_seq:06d}",
                        datetime.combine(day, datetime.min.time()).replace(hour=rng.randint(0, 23), minute=rng.randint(0, 59)),
                        eq_id,
                        ws.workshop_id,
                        code,
                        sev,
                        f"{name} 触发 {code} 告警",
                        float(rng.randint(5, 120)),
                    )
                )

        day += timedelta(days=1)
        day_index += 1

    con.executemany("INSERT INTO production_record VALUES (?, ?, ?, ?, ?, ?, ?)", production_rows)
    con.executemany("INSERT INTO energy_record VALUES (?, ?, ?, ?, ?, ?)", energy_rows)
    con.executemany("INSERT INTO equipment_operation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", operation_rows)
    con.executemany("INSERT INTO equipment_alarm VALUES (?, ?, ?, ?, ?, ?, ?, ?)", alarm_rows)

    _verify(con)
    con.close()
    return db_path


def _verify(con: duckdb.DuckDBPyConnection) -> None:
    """打印关键校验：异常窗口的单位电耗确实上升，且产量基本持平。"""
    sql = """
    WITH e AS (
        SELECT workshop_id, record_date, SUM(quantity) AS kwh
        FROM energy_record WHERE energy_type = 'electricity'
        GROUP BY 1, 2
    ), p AS (
        SELECT workshop_id, record_date, SUM(output_qty) AS qty
        FROM production_record GROUP BY 1, 2
    )
    SELECT e.workshop_id,
           ROUND(AVG(CASE WHEN e.record_date BETWEEN DATE '2026-08-28' AND DATE '2026-09-26' THEN e.kwh / NULLIF(p.qty,0) END), 3) AS base_unit,
           ROUND(AVG(CASE WHEN e.record_date BETWEEN DATE '2026-09-27' AND DATE '2026-10-26' THEN e.kwh / NULLIF(p.qty,0) END), 3) AS curr_unit,
           ROUND(AVG(CASE WHEN e.record_date BETWEEN DATE '2026-08-28' AND DATE '2026-09-26' THEN p.qty END), 1) AS base_qty,
           ROUND(AVG(CASE WHEN e.record_date BETWEEN DATE '2026-09-27' AND DATE '2026-10-26' THEN p.qty END), 1) AS curr_qty
    FROM e JOIN p USING (workshop_id, record_date)
    GROUP BY 1 ORDER BY 1
    """
    rows = con.execute(sql).fetchall()
    print(f"数据生成完成：{START_DATE} ~ {END_DATE}")
    for ws_id, base_unit, curr_unit, base_qty, curr_qty in rows:
        delta = (curr_unit / base_unit - 1) * 100 if base_unit else 0
        print(
            f"  {ws_id}: 单位电耗 {base_unit} → {curr_unit} kWh/件 ({delta:+.1f}%)，"
            f"日均产量 {base_qty} → {curr_qty}"
        )
    for table in ("workshop", "production_record", "energy_record", "equipment", "equipment_operation", "equipment_alarm"):
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:22s} {count:6d} 行")


def main() -> None:
    path = build()
    print(f"数据库：{path}")


if __name__ == "__main__":
    main()
