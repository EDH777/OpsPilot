"""测试夹具：隔离运行目录、确保数据库就绪、屏蔽真实模型调用。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="opspilot-test-"))

# 必须在导入 opspilot 之前设置，避免污染真实 runs/ 与命中真实模型
os.environ["OPSPILOT_RUNS_DIR"] = str(_TMP / "runs")
os.environ["OPSPILOT_STATE_DB_PATH"] = str(_TMP / "runs" / "state.sqlite3")
os.environ["OPSPILOT_LLM_API_KEY"] = ""
os.environ["OPSPILOT_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["OPSPILOT_BUSINESS_API_BASE"] = "http://127.0.0.1:8101"
os.environ["OPSPILOT_SANDBOX"] = "subprocess"
os.environ["OPSPILOT_SANDBOX_TIMEOUT_S"] = "20"


@pytest.fixture(scope="session", autouse=True)
def seeded_database() -> Path:
    from opspilot.config import get_settings
    from opspilot.data.seed import build

    db = get_settings().db_path
    if not db.exists():
        build()
    return db


@pytest.fixture
def settings(tmp_path: Path):
    from opspilot.config import Settings

    base = Settings()
    return base.model_copy(
        update={
            "runs_dir": tmp_path / "runs",
            "state_db_path": tmp_path / "state.sqlite3",
            "max_steps": 8,
            "max_tool_calls": 12,
            "max_tokens": 60_000,
            "context_token_budget": 12_000,
        }
    )


@pytest.fixture
def registry():
    from opspilot.harness.registry import build_default_registry

    return build_default_registry()


@pytest.fixture
async def state_store(settings):
    from opspilot.harness.state import StateStore

    store = StateStore(settings.state_db_path)
    yield store
    store.close()
