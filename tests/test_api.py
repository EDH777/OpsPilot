"""API 集成测试：健康检查、驾驶舱、SSE 调查与看板推送、报告导出。"""

from __future__ import annotations

import asyncio
import json
import shutil
import time

import duckdb
import pytest
from fastapi.testclient import TestClient

from opspilot.api.app import app
from opspilot.services.data_version import data_signature


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
    assert body["data_version"].startswith("v")
    assert body["generated_at"]


def test_data_signature_tracks_writes(tmp_path):
    db = tmp_path / "sig.duckdb"
    db.write_bytes(b"a")
    first = data_signature(db)
    time.sleep(0.01)
    db.write_bytes(b"bb")
    second = data_signature(db)
    assert first.version != second.version
    assert second.size == 2


async def test_dashboard_stream_pushes_on_db_change(tmp_path, monkeypatch):
    import opspilot.api.app as appmod
    from opspilot.config import get_settings

    src = get_settings().db_path
    db = tmp_path / "stream.duckdb"
    shutil.copy(src, db)
    patched = appmod.settings.model_copy(update={"db_path": db})
    monkeypatch.setattr(appmod, "settings", patched)

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    gen = appmod.dashboard_event_stream(FakeRequest(), days=30, poll_s=0.2)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=8)
        first_payload = json.loads(first.split("data: ", 1)[1])
        assert first_payload["reason"] == "initial"
        version1 = first_payload["data_version"]

        con = duckdb.connect(str(db))
        con.execute("UPDATE workshop SET workshop_name = '一号车间(改)' WHERE workshop_id = 'WS-01'")
        con.close()

        second = await asyncio.wait_for(gen.__anext__(), timeout=12)
        payload = json.loads(second.split("data: ", 1)[1])
        assert payload["reason"] == "changed"
        assert payload["data_version"] != version1
        assert "一号车间(改)" in payload["workshop_compare"]["workshops"]
    finally:
        await gen.aclose()


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


def _sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    return events


def _run_once(client, message: str, session_id: str | None = None) -> tuple[str, list[dict]]:
    payload = {"message": message, "mock": True}
    if session_id:
        payload["session_id"] = session_id
    with client.stream("POST", "/api/chat", json=payload) as resp:
        events = _sse_events("".join(resp.iter_text()))
    started = next(e for e in events if e["type"] == "run_started")
    return started["data"]["session_id"], events


def test_sessions_group_multiple_runs(client):
    sid, _ = _run_once(client, "看一下二号车间这个月的用电情况")
    sid2, _ = _run_once(client, "那看一下9月份的", sid)
    assert sid2 == sid, "同一会话必须复用 session_id"

    sessions = client.get("/api/sessions").json()["sessions"]
    item = next(s for s in sessions if s["session_id"] == sid)
    assert item["runs"] >= 2
    assert item["title"] == "看一下二号车间这个月的用电情况"

    runs = client.get(f"/api/sessions/{sid}").json()["runs"]
    tasks = [r["task"] for r in runs]
    assert tasks[-2:] == ["看一下二号车间这个月的用电情况", "那看一下9月份的"]
    assert runs[-1]["session_id"] == sid


def test_session_detail_404(client):
    assert client.get("/api/sessions/sess_not_exists").status_code == 404
