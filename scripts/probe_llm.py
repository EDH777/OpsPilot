"""探测 OpenAI 兼容端点：/models 列表 + 一次带 function calling 的最小请求。

用法： & "D:\\Anaconda3\\envs\\chAgent\\python.exe" scripts/probe_llm.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("OPSPILOT_")})
    return env


def main() -> int:
    env = load_env()
    base = env.get("OPSPILOT_LLM_BASE_URL", "").rstrip("/")
    key = env.get("OPSPILOT_LLM_API_KEY", "")
    model = env.get("OPSPILOT_LLM_MODEL", "")
    if not (base and key and model):
        print("缺少 OPSPILOT_LLM_* 配置")
        return 2

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    print(f"base={base}\nmodel={model}\nkey_len={len(key)} key_prefix={key[:6]}")

    # 注意：本机 NO_PROXY 含 "[::1]"，httpx 解析会抛 InvalidURL，故不走环境代理。
    with httpx.Client(timeout=30.0, trust_env=False) as c:
        r = c.get(f"{base}/models", headers=headers)
        print(f"\n[GET /models] {r.status_code}")
        print(r.text[:800])

        body = {
            "model": model,
            "messages": [{"role": "user", "content": "只回复两个字：收到"}],
            "max_tokens": 32,
            "temperature": 0,
        }
        r = c.post(f"{base}/chat/completions", headers=headers, json=body)
        print(f"\n[POST /chat/completions] {r.status_code}")
        print(r.text[:1200])

        body2 = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是数据助手，需要查数据时调用工具。"},
                {"role": "user", "content": "帮我查一下三号车间最近的电耗"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_sql",
                        "description": "执行只读 SQL 查询",
                        "parameters": {
                            "type": "object",
                            "properties": {"sql": {"type": "string"}},
                            "required": ["sql"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "max_tokens": 200,
            "temperature": 0,
        }
        r = c.post(f"{base}/chat/completions", headers=headers, json=body2)
        print(f"\n[POST function-calling] {r.status_code}")
        data = r.json()
        msg = data["choices"][0]["message"]
        print(json.dumps(msg, ensure_ascii=False, indent=2)[:1200])
        print("tool_calls:", bool(msg.get("tool_calls")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
