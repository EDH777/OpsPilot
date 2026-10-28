"""运行统计聚合测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from opspilot.modules.metrics.service import compute_metrics


def _write_trace(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def test_compute_metrics_aggregates_status_tokens_tools(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    now = time.time()
    _write_trace(
        runs_dir / "r1" / "trace.jsonl",
        [
            {"type": "tool_finished", "data": {"name": "run_sql", "ok": True, "latency_ms": 30}},
            {"type": "tool_finished", "data": {"name": "run_sql", "ok": True, "latency_ms": 50}},
        ],
    )
    _write_trace(
        runs_dir / "r2" / "trace.jsonl",
        [
            {
                "type": "tool_finished",
                "data": {
                    "name": "query_equipment_api",
                    "ok": False,
                    "latency_ms": 20,
                    "error": {"kind": "invalid_argument"},
                },
            }
        ],
    )

    rows = [
        {
            "run_id": "r1",
            "status": "completed",
            "step": 4,
            "usage": {"total_tokens": 1000},
            "created_at": now - 120,
            "updated_at": now - 100,
        },
        {
            "run_id": "r2",
            "status": "failed",
            "step": 2,
            "usage": {"total_tokens": 500},
            "created_at": now - 60,
            "updated_at": now - 50,
        },
        {
            "run_id": "r3",
            "status": "running",
            "step": 1,
            "usage": {"total_tokens": 100},
            "created_at": now - 10,
            "updated_at": now - 10,
        },
    ]

    m = compute_metrics(rows, runs_dir, days=7)

    assert m["window"]["runs"] == 3
    assert len(m["daily"]) == 7
    assert m["totals"]["finished"] == 2
    assert m["totals"]["completed"] == 1
    assert m["totals"]["tool_calls"] == 3
    assert m["totals"]["tool_failures"] == 1

    success = next(k for k in m["kpis"] if k["key"] == "success_rate")
    assert success["value"] == 50.0

    tools = {t["name"]: t for t in m["tools"]}
    assert tools["run_sql"]["calls"] == 2
    assert tools["run_sql"]["avg_latency_ms"] == 40.0
    assert tools["query_equipment_api"]["failures"] == 1
    assert m["error_kinds"][0]["kind"] == "invalid_argument"

    statuses = {s["status"]: s["count"] for s in m["status_counts"]}
    assert statuses == {"completed": 1, "failed": 1, "running": 1}


def test_compute_metrics_empty(tmp_path: Path):
    m = compute_metrics([], tmp_path / "runs", days=7)
    assert m["window"]["runs"] == 0
    assert len(m["daily"]) == 7
    assert m["tools"] == []
    assert m["totals"]["finished"] == 0
    assert next(k for k in m["kpis"] if k["key"] == "success_rate")["value"] == 0.0
