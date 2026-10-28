"""轻量权限体系测试：2 角色、数据域、成本隔离、对象归属、审计。"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from opspilot.api.app import app
from opspilot.core.loop import AgentLoop
from opspilot.harness.errors import ToolError
from opspilot.harness.registry import ToolContext
from opspilot.llm.mock import ScriptedLLM, assistant, tool_call
from opspilot.security.data import ensure_scoped_snapshot, guard_scoped_sql
from opspilot.security.model import ROLES
from opspilot.tools import schema, sql


@pytest.fixture
def manager_ctx(settings, tmp_path: Path, manager_principal, audit) -> ToolContext:
    ctx = ToolContext(
        run_id="run_mgr",
        principal=manager_principal,
        audit=audit,
        settings=settings,
        artifacts_dir=tmp_path / "artifacts",
    )
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return ctx


@pytest.fixture
def boss_ctx(settings, tmp_path: Path, principal, audit) -> ToolContext:
    ctx = ToolContext(
        run_id="run_boss",
        principal=principal,
        audit=audit,
        settings=settings,
        artifacts_dir=tmp_path / "artifacts",
    )
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return ctx


def _login(client: TestClient, username: str) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "OpsPilot@2026"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 数据域


async def test_manager_sql_only_own_workshop(manager_ctx):
    result = await sql.run_sql(
        sql.RunSqlArgs(sql="SELECT DISTINCT workshop_id FROM production_record"),
        manager_ctx,
    )
    assert result["rows"] == [["WS-03"]]


async def test_manager_cannot_see_cost_column(manager_ctx):
    result = await sql.run_sql(
        sql.RunSqlArgs(sql="SELECT * FROM energy_record LIMIT 1"), manager_ctx
    )
    assert "cost_cny" not in result["columns"]


async def test_manager_cannot_query_cost_table(manager_ctx):
    # 视图里没有 workshop_cost，DuckDB 会报 catalog error；关键是没有数据返回。
    with pytest.raises(Exception):
        await sql.run_sql(sql.RunSqlArgs(sql="SELECT * FROM workshop_cost"), manager_ctx)


async def test_manager_schema_hides_cost(manager_ctx):
    result = await schema.inspect_schema(schema.InspectSchemaArgs(), manager_ctx)
    names = {t["name"] for t in result["tables"]}
    assert "workshop_cost" not in names
    energy = next(t for t in result["tables"] if t["name"] == "energy_record")
    assert "cost_cny" not in {c["name"] for c in energy["columns"]}


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM raw.production_record",
        "SELECT * FROM 'data/opspilot.duckdb'",
        "SELECT * FROM query_table('raw.workshop_cost')",
        "SELECT * FROM read_parquet('x.parquet')",
    ],
)
def test_manager_sql_guard_blocks_bypasses(manager_ctx, statement):
    with pytest.raises(ToolError):
        guard_scoped_sql(statement, manager_ctx.principal)


async def test_manager_python_snapshot_is_scoped(manager_ctx):
    path = ensure_scoped_snapshot(manager_ctx)
    con = duckdb.connect(str(path), read_only=True)
    try:
        assert con.execute(
            "SELECT DISTINCT workshop_id FROM production_record"
        ).fetchall() == [("WS-03",)]
        assert (
            con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='workshop_cost'"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()


async def test_boss_can_query_cost(boss_ctx):
    from opspilot.tools import cost

    result = await cost.query_cost_summary(cost.CostQueryArgs(month="2026-10"), boss_ctx)
    assert result["ok"] is True
    assert len(result["rows"]) == 5


def test_tool_catalog_differs_by_role():
    assert "query_cost_summary" in ROLES["executive"].tools
    assert "query_cost_summary" not in ROLES["workshop_manager"].tools


# ---------------------------------------------------------------- 对象归属 / 认证


def test_auth_required_and_tampered_cookie():
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 401
        client.cookies.set("opspilot_session", "bad.token")
        assert client.get("/api/runs").status_code == 401


def test_manager_catalog_dashboard_and_audit():
    with TestClient(app) as client:
        _login(client, "zhaolei")
        names = [t["name"] for t in client.get("/api/tools").json()["tools"]]
        assert "query_cost_summary" not in names
        assert len(names) == 9
        dash = client.get("/api/dashboard?days=30").json()
        assert dash["workshop_compare"]["workshops"] == ["三号车间"]
        assert client.get("/api/audit").status_code == 403
        me = client.get("/api/auth/me").json()
        assert me["scope"]["workshops"] == ["WS-03"]


def test_cross_user_object_access_returns_404():
    with TestClient(app) as boss, TestClient(app) as manager:
        _login(boss, "boss")
        _login(manager, "zhaolei")
        with boss.stream(
            "POST",
            "/api/chat",
            json={"message": "三号车间近30天单位电耗为什么升高？", "mock": True},
        ) as resp:
            "".join(resp.iter_text())
        reports = boss.get("/api/reports").json()["reports"]
        assert reports
        run_id = reports[0]["run_id"]
        charts = reports[0]["report"].get("charts") or []

        assert manager.get(f"/api/runs/{run_id}").status_code == 404
        assert manager.get(f"/api/runs/{run_id}/events").status_code == 404
        assert manager.get(f"/api/runs/{run_id}/report.md").status_code == 404
        if charts:
            assert (
                manager.get(f"/artifacts/{run_id}/{charts[0]}").status_code == 404
            )


def test_report_meta_is_stamped():
    with TestClient(app) as client:
        _login(client, "zhaolei")
        with client.stream(
            "POST",
            "/api/chat",
            json={"message": "三号车间近30天单位电耗为什么升高？", "mock": True},
        ) as resp:
            "".join(resp.iter_text())
        reports = client.get("/api/reports").json()["reports"]
        assert reports
        meta = reports[0]["report"]["_meta"]
        assert meta["owner"]["user_id"] == "u_zhaolei"
        assert meta["owner"]["role"] == "workshop_manager"
        assert meta["scope"]["workshops"] == ["WS-03"]


# ---------------------------------------------------------------- 对抗模型


async def test_adversarial_manager_model_is_denied(
    settings, state_store, registry, manager_principal
):
    events: list[dict] = []
    script = [
        assistant(
            tool_calls=[
                tool_call(
                    "run_sql",
                    {"sql": "SELECT * FROM production_record WHERE workshop_id='WS-05'"},
                )
            ]
        ),
        assistant(tool_calls=[tool_call("query_cost_summary", {"month": "2026-10"})]),
        assistant(
            tool_calls=[
                tool_call(
                    "submit_report",
                    {
                        "title": "五号车间调查",
                        "summary": "WS-05 不良率升至 4.5%，这是一个足够长的摘要。",
                        "findings": [
                            {
                                "statement": "WS-05 不良率 4.5%",
                                "evidence": "production_record",
                                "confidence": "已验证事实",
                            }
                        ],
                    },
                )
            ]
        ),
        assistant("完成"),
    ]
    loop = AgentLoop(ScriptedLLM(script), settings=settings, state=state_store, registry=registry)
    result = await loop.run(
        "查一下五号车间的不良率",
        principal=manager_principal,
        emit=lambda e: events.append(e.to_dict()),
    )

    assert result.status.value == "completed"
    assert result.report is None
    denied = [e for e in events if e["type"] == "policy_denied"]
    assert denied, "越界 SQL 必须产生 policy_denied 事件，trace 才能显示拒绝"
    assert any("车间" in (e["data"].get("message") or "") for e in denied)

    audit_path = Path(settings.runs_dir) / "audit.jsonl"
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    denies = [e for e in entries if e.get("decision") == "deny"]
    assert len(denies) >= 3
    assert any("车间" in (e.get("reason") or "") for e in denies)
