"""临时验证：SSE 看板推送是否真的随数据库变化（验证后自动还原）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import httpx

DB = str(Path(__file__).resolve().parents[2] / "data" / "opspilot.duckdb")


def read_event(lines) -> tuple[str | None, dict]:
    event = None
    for line in lines:
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            return event, json.loads(line[6:])
    raise RuntimeError("流已关闭")


def write(sql: str) -> None:
    last = None
    for attempt in range(5):
        try:
            con = duckdb.connect(DB)
            con.execute(sql)
            con.close()
            return
        except Exception as exc:  # 锁冲突时重试
            last = exc
            time.sleep(0.4)
    raise last  # type: ignore[misc]


def total_output(payload: dict) -> float:
    return next(k["value"] for k in payload["kpis"] if k["key"] == "output")


def main() -> None:
    with httpx.Client(timeout=httpx.Timeout(60.0), trust_env=False) as c:
        c.post(
            "http://127.0.0.1:8100/api/auth/login",
            json={"username": "boss", "password": "OpsPilot@2026"},
        )
        with c.stream("GET", "http://127.0.0.1:8100/api/dashboard/stream?days=30") as r:
            lines = r.iter_lines()
            ev1, p1 = read_event(lines)
            print(f"[{ev1}] version={p1['data_version']} reason={p1['reason']} 总产量={total_output(p1)}")

            write("UPDATE production_record SET output_qty = output_qty + 5000 "
                  "WHERE workshop_id='WS-04' AND record_date = DATE '2026-10-26'")
            started = time.perf_counter()
            ev2, p2 = read_event(lines)
            elapsed = time.perf_counter() - started
            print(f"[{ev2}] version={p2['data_version']} reason={p2['reason']} "
                  f"总产量={total_output(p2)} 推送延迟={elapsed:.1f}s")

            write("UPDATE production_record SET output_qty = output_qty - 5000 "
                  "WHERE workshop_id='WS-04' AND record_date = DATE '2026-10-26'")
            ev3, p3 = read_event(lines)
            print(f"[{ev3}] version={p3['data_version']} reason={p3['reason']} "
                  f"总产量={total_output(p3)}（还原）")


if __name__ == "__main__":
    main()
