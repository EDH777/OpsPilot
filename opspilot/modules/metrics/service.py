"""运行指标聚合：给"运行统计"页提供数据。

数据来源：
- SQLite `runs`：状态、步数、token、耗时；
- `runs/<run_id>/trace.jsonl`：工具调用次数、失败、耗时与错误类型。

trace 解析按 (文件, mtime) 做缓存，避免每次刷新都重读全部 JSONL。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

FINISHED = {"completed", "failed", "interrupted", "budget_exceeded", "cancelled"}
FAILED = {"failed", "interrupted", "budget_exceeded", "cancelled"}


@lru_cache(maxsize=1024)
def _scan_trace(path_str: str, _mtime_ns: int) -> tuple[tuple[str, bool, float, str | None], ...]:
    """返回 (工具名, 是否成功, 耗时ms, 错误类型) 列表；mtime 变了缓存自然失效。"""
    path = Path(path_str)
    if not path.exists():
        return ()
    hits: list[tuple[str, bool, float, str | None]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "tool_finished":
                continue
            data = event.get("data") or {}
            error = data.get("error") or {}
            hits.append(
                (
                    str(data.get("name") or "unknown"),
                    bool(data.get("ok")),
                    float(data.get("latency_ms") or 0),
                    error.get("kind") if error else None,
                )
            )
    except OSError:
        return ()
    return tuple(hits)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def compute_metrics(rows: list[dict[str, Any]], runs_dir: Path, *, days: int = 30) -> dict[str, Any]:
    now = datetime.now()
    total = len(rows)
    finished = [r for r in rows if r.get("status") in FINISHED]
    completed = [r for r in rows if r.get("status") == "completed"]

    status_counts: dict[str, int] = {}
    tokens_total = 0
    steps_total = 0
    durations: list[float] = []
    daily: dict[str, dict[str, Any]] = {}

    tool_stats: dict[str, dict[str, Any]] = {}
    error_kinds: dict[str, int] = {}

    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        usage = row.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        tokens_total += tokens
        steps_total += int(row.get("step") or 0)

        created = datetime.fromtimestamp(float(row.get("created_at") or 0))
        day_key = created.strftime("%Y-%m-%d")
        bucket = daily.setdefault(day_key, {"date": day_key, "runs": 0, "failed": 0, "tokens": 0})
        bucket["runs"] += 1
        bucket["tokens"] += tokens
        if status in FAILED:
            bucket["failed"] += 1

        if status in FINISHED and row.get("updated_at"):
            durations.append(max(0.0, (float(row["updated_at"]) - float(row["created_at"])) * 1000))

        trace_path = Path(runs_dir) / str(row["run_id"]) / "trace.jsonl"
        try:
            mtime_ns = trace_path.stat().st_mtime_ns
        except OSError:
            continue
        for name, ok, latency, kind in _scan_trace(str(trace_path), mtime_ns):
            item = tool_stats.setdefault(
                name, {"name": name, "calls": 0, "failures": 0, "latency_ms": 0.0}
            )
            item["calls"] += 1
            item["latency_ms"] += latency
            if not ok:
                item["failures"] += 1
                key = kind or "unknown"
                error_kinds[key] = error_kinds.get(key, 0) + 1

    tools = []
    for item in tool_stats.values():
        calls = max(1, item["calls"])
        tools.append(
            {
                "name": item["name"],
                "calls": item["calls"],
                "failures": item["failures"],
                "failure_rate": round(item["failures"] / calls, 4),
                "avg_latency_ms": round(item["latency_ms"] / calls, 1),
            }
        )
    tools.sort(key=lambda x: x["calls"], reverse=True)

    # 补零：让折线在没运行的日子也连续
    series: list[dict[str, Any]] = []
    if days > 0:
        for offset in range(days - 1, -1, -1):
            key = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            series.append(daily.get(key) or {"date": key, "runs": 0, "failed": 0, "tokens": 0})
    else:
        series = [daily[key] for key in sorted(daily)]

    tool_calls = sum(t["calls"] for t in tools)
    tool_failures = sum(t["failures"] for t in tools)
    success_rate = (len(completed) / len(finished)) if finished else 0.0

    return {
        "window": {
            "days": days,
            "runs": total,
            "since": (now - timedelta(days=days)).isoformat(timespec="seconds") if days else None,
        },
        "kpis": [
            {"key": "runs", "label": "运行总数", "value": total, "unit": "次"},
            {
                "key": "success_rate",
                "label": "成功率",
                "value": round(success_rate * 100, 1),
                "unit": "%",
            },
            {
                "key": "avg_duration",
                "label": "平均耗时",
                "value": round(sum(durations) / len(durations) / 1000, 2) if durations else 0,
                "unit": "秒",
            },
            {
                "key": "tokens",
                "label": "Token 总量",
                "value": tokens_total,
                "unit": "",
            },
            {
                "key": "tool_failure_rate",
                "label": "工具失败率",
                "value": round(tool_failures / tool_calls * 100, 1) if tool_calls else 0,
                "unit": "%",
            },
        ],
        "status_counts": [
            {"status": status, "count": count}
            for status, count in sorted(status_counts.items(), key=lambda x: -x[1])
        ],
        "daily": series,
        "tools": tools,
        "error_kinds": [
            {"kind": kind, "count": count}
            for kind, count in sorted(error_kinds.items(), key=lambda x: -x[1])
        ],
        "totals": {
            "finished": len(finished),
            "completed": len(completed),
            "steps": steps_total,
            "steps_avg": round(steps_total / total, 2) if total else 0,
            "tokens_avg": round(tokens_total / total) if total else 0,
            "duration_p95_ms": round(_p95(durations), 1),
            "tool_calls": tool_calls,
            "tool_failures": tool_failures,
        },
        "generated_at": now.isoformat(timespec="seconds"),
    }
