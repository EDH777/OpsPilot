"""API 集成测试：健康检查、驾驶舱、SSE 调查、报告导出。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from opspilot.api.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"]["ready"] is True


def test_tool_catalog(client):
    r = client.get("/api/tools")
    names = {t["name"] for t in r.json()["tools"]}
    assert {"inspect_schema", "run_sql", "python_analysis", "submit_report"} <= names


def test_dashboard(client):
    r = client.get("/api/dashboard?days=30")
    assert r.status_code == 200
    body = r.json()
    assert len(body["kpis"]) == 5
    assert body["unit_energy_trend"]["dates"]
    assert body["unit_energy_trend"]["series"]
    assert any(a["workshop_id"] == "WS-03" for a in body["anomalies"])


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "OpsPilot" in r.text


def test_chat_stream_with_mock_model(client):
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "三号车间近30天单位产品电耗为什么升高？", "mock": True},
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())

    assert '"type": "run_started"' in text
    assert '"type": "tool_finished"' in text
    assert '"type": "report"' in text
    assert '"type": "run_finished"' in text

    # 报告已落库并可导出
    reports = client.get("/api/reports").json()["reports"]
    assert reports
    run_id = reports[0]["run_id"]
    md = client.get(f"/api/runs/{run_id}/report.md")
    assert md.status_code == 200
    assert "三号车间" in md.text
    assert "结论与证据" in md.text

    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert any(e["type"] == "tool_finished" for e in events)


def test_chat_rejects_empty_message(client):
    r = client.post("/api/chat", json={"message": "x"})
    assert r.status_code == 422
