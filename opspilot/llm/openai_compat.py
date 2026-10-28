"""OpenAI 兼容客户端（DeepSeek / 阿里 MAAS / vLLM / Ollama 等通用）。

用 httpx 直接发请求而不是 openai SDK，原因有三：
1. 需要读取 qwen 系列返回的 reasoning_content 等非标准字段；
2. 需要精确控制重试、超时和代理行为（本机 NO_PROXY 会让 httpx 的 trust_env 崩溃）；
3. 保持"自研执行层、供应商可替换"的边界清晰。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from opspilot.config import Settings
from opspilot.core.types import Message, ToolCall, Usage
from opspilot.llm.base import BaseLLM, LLMError, LLMResponse

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatLLM(BaseLLM):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.llm_model
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=15.0),
            trust_env=settings.llm_use_env_proxy,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [m.to_openai() for m in messages],
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        data = await self._post_with_retry("/chat/completions", payload)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return self._parse(data, latency_ms)

    # ------------------------------------------------------------------ 内部

    async def _post_with_retry(self, path: str, payload: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = await self._client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last = exc
                if i == attempts - 1:
                    raise LLMError(f"模型服务不可达：{exc}", retryable=True) from exc
                await asyncio.sleep(0.8 * (2**i))
                continue

            if r.status_code == 200:
                try:
                    return r.json()
                except json.JSONDecodeError as exc:
                    raise LLMError(f"模型返回非 JSON：{r.text[:300]}", status=200) from exc

            body = r.text[:800]
            retryable = r.status_code in RETRYABLE_STATUS
            if retryable and i < attempts - 1:
                await asyncio.sleep(0.8 * (2**i))
                continue
            hint = {
                401: "API Key 无效或已过期",
                403: "无权访问该模型",
                404: "接口路径或模型名不存在",
                429: "触发限流，请降低并发或稍后重试",
            }.get(r.status_code, "")
            snippet = body.replace("\n", " ")[:300]
            raise LLMError(
                f"模型调用失败 {r.status_code} {hint}".strip() + (f"：{snippet}" if snippet else ""),
                status=r.status_code,
                retryable=retryable,
                body=body,
            )
        raise LLMError(f"模型调用失败：{last}", retryable=True)

    def _parse(self, data: dict[str, Any], latency_ms: float) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("模型返回缺少 choices", body=json.dumps(data, ensure_ascii=False)[:500])
        choice = choices[0]
        msg = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(tool_calls)}",
                    name=fn.get("name") or "",
                    arguments=args or "{}",
                )
            )

        content = msg.get("content")
        if isinstance(content, list):  # 少数网关返回分段内容
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if content:
            # 部分推理模型会把思考结束标记漏进 content
            content = re.sub(r"^\s*(</think\s*>|<\|endofthought\|>)\s*", "", content)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=Usage.from_openai(data.get("usage")),
            reasoning=msg.get("reasoning_content") or msg.get("reasoning"),
            finish_reason=choice.get("finish_reason"),
            model=data.get("model") or self.model_name,
            latency_ms=latency_ms,
            raw=data,
        )
