"""业务指标口径（指标字典）。

LLM 容易"自由发挥"指标定义——单位产品电耗到底是分子是总电还是生产用电、分母是产量还是
合格产量、单位是 kWh/件还是 kWh/吨。把口径写死在代码里，通过工具和提示词同时下发，
是数据 Agent 与"看起来聪明"的本质区别之一。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    formula: str
    unit: str
    notes: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name, "formula": self.formula, "unit": self.unit, "notes": self.notes}


METRICS: dict[str, Metric] = {
    "unit_energy": Metric(
        key="unit_energy",
        name="单位产品电耗",
        formula="车间电力消耗总量(kWh) / 该车间合格产量(件)",
        unit="kWh/件",
        notes="合格产量 = 产量 - 不良数；产量为 0 时该指标无定义，必须显式说明而不是填 0。",
    ),
    "output_completion": Metric(
        key="output_completion",
        name="产量完成率",
        formula="实际产量 / 计划产量",
        unit="%",
        notes="按同日期、同车间的合计产量与合计计划量计算。",
    ),
    "idle_rate": Metric(
        key="idle_rate",
        name="设备空转率",
        formula="空转时长(min) / (运行时长(min) + 空转时长(min))",
        unit="%",
        notes="空转 = 设备通电但无有效产出；空转率升高是单位电耗上升的典型原因。",
    ),
    "defect_rate": Metric(
        key="defect_rate",
        name="不良率",
        formula="不良数 / 产量",
        unit="%",
    ),
    "load_rate": Metric(
        key="load_rate",
        name="设备平均负载率",
        formula="按设备逐日负载率的算术平均（0~1）",
        unit="",
        notes="负载率显著下降且空转率上升，通常意味着设备在低效状态下运行。",
    ),
}


def glossary_text() -> str:
    lines = ["【指标口径字典】（计算时必须严格遵循，不得自行改口径）"]
    for m in METRICS.values():
        line = f"- {m.name}（{m.key}）= {m.formula}，单位 {m.unit or '无'}"
        if m.notes:
            line += f"；注：{m.notes}"
        lines.append(line)
    return "\n".join(lines)


def glossary_dict() -> list[dict[str, str]]:
    return [m.to_dict() for m in METRICS.values()]
