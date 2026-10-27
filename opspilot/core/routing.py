"""任务分级：把"看一眼"和"查原因"分开，避免用调查级流程处理简单查询。

规则刻意保持简单、可解释、可测试——不需要再用一次 LLM 调用做分类，
否则为了省 token 反而先花一次 token。
"""

from __future__ import annotations

from enum import Enum


class TaskProfile(str, Enum):
    SIMPLE = "simple"
    INVESTIGATION = "investigation"

    @property
    def label(self) -> str:
        return {"simple": "简单查询", "investigation": "异常调查"}[self.value]


# 出现这些词，按"要解释原因"处理
_INVESTIGATION_KEYWORDS = (
    "为什么",
    "原因",
    "调查",
    "排查",
    "异常",
    "归因",
    "根因",
    "相关性",
    "有没有关系",
    "是否有关",
    "影响",
    "嫌疑",
    "分析一下",
    "深入",
)

# 出现这些词，按"取数/汇总"处理
_SIMPLE_KEYWORDS = (
    "看一下",
    "看看",
    "查一下",
    "查询",
    "多少",
    "情况",
    "汇总",
    "统计",
    "列表",
    "排名",
    "是多少",
    "显示",
    "列一下",
)


def classify_task(task: str) -> TaskProfile:
    text = (task or "").strip().lower()
    if any(k in text for k in _INVESTIGATION_KEYWORDS):
        return TaskProfile.INVESTIGATION
    if any(k in text for k in _SIMPLE_KEYWORDS):
        return TaskProfile.SIMPLE
    # 拿不准时按调查处理：宁可多花一点，也不要漏掉关键证据
    return TaskProfile.INVESTIGATION
