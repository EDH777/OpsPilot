from opspilot.config import Settings
from opspilot.llm.base import BaseLLM, LLMError, LLMResponse
from opspilot.llm.mock import EchoLLM, ScriptedLLM, assistant, tool_call
from opspilot.llm.openai_compat import OpenAICompatLLM


def build_llm(settings: Settings, *, mock: bool = False) -> BaseLLM:
    """按配置构建模型客户端。mock=True 或未配置 Key 时返回脚本模型。"""
    if mock:
        return ScriptedLLM([], repeat_last=False)
    if not settings.has_llm:
        raise LLMError("未配置 OPSPILOT_LLM_API_KEY；可用 --mock 运行离线演示")
    return OpenAICompatLLM(settings)


__all__ = [
    "BaseLLM",
    "EchoLLM",
    "LLMError",
    "LLMResponse",
    "OpenAICompatLLM",
    "ScriptedLLM",
    "assistant",
    "build_llm",
    "tool_call",
]
