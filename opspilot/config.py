"""集中配置。所有可调参数通过 OPSPILOT_* 环境变量或 .env 注入，不硬编码密钥。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSPILOT_",
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 模型 ----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.1
    # 注意：qwen 等推理模型的 reasoning tokens 也计入 completion，
    # 设太小会把长 SQL / Python 代码截断成非法 JSON。
    llm_max_tokens: int = 8192
    llm_timeout_s: float = 120.0
    llm_use_env_proxy: bool = False  # 本机 NO_PROXY 含 "[::1]"，httpx 解析会崩，默认绕过环境代理

    # ---- 数据与产物 ----
    db_path: Path = ROOT / "data" / "opspilot.duckdb"
    runs_dir: Path = ROOT / "runs"
    state_db_path: Path = ROOT / "runs" / "state.sqlite3"

    # ---- 预算（Harness 硬约束）----
    max_steps: int = 24
    max_tool_calls: int = 40
    max_tokens: int = 180_000
    max_seconds: float = 900.0
    context_token_budget: int = 30_000
    # 简单查询（"看一下/汇总"类）单独给一档更紧的预算
    simple_max_steps: int = 6
    simple_max_tool_calls: int = 8
    simple_max_tokens: int = 80_000
    simple_max_seconds: float = 300.0
    simple_context_token_budget: int = 16_000
    sql_max_rows: int = 500
    sql_preview_rows: int = 20
    sql_summary_threshold: int = 50
    sql_timeout_s: float = 20.0
    tool_timeout_s: float = 60.0

    # ---- 沙箱 ----
    sandbox: Literal["subprocess", "docker"] = "subprocess"
    sandbox_timeout_s: float = 30.0
    sandbox_image: str = "python:3.11-slim"

    # ---- 业务 API（模拟 ERP/MES）----
    business_api_base: str = "http://127.0.0.1:8101"
    business_api_token: str = ""

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8100
    cors_origins: str = "*"

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """进程内单例；测试可用 refresh=True 重新读取。"""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
