"""验收评测：跑任务集、检查证据与安全边界、输出结果 JSON + Markdown 表。

用法：
    python -m evals.run_eval                       # 全部任务，真实模型
    python -m evals.run_eval --limit 2             # 只跑前 2 个
    python -m evals.run_eval --only t5_safety      # 只跑指定任务
    python -m evals.run_eval --dry-run             # 只打印任务，不调用模型
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb

from opspilot.config import get_settings
from opspilot.core.loop import AgentLoop
from opspilot.llm import build_llm

TASKS_PATH = ROOT / "evals" / "tasks.json"
RESULTS_DIR = ROOT / "evals" / "results"


def load_tasks() -> list[dict[str, Any]]:
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))["tasks"]


def row_count(table: str) -> int:
    con = duckdb.connect(str(get_settings().db_path), read_only=True)
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        con.close()


def evaluate(task: dict[str, Any], result, before_counts: dict[str, int]) -> dict[str, Any]:
    report_text = json.dumps(result.report or {}, ensure_ascii=False)
    answer_text = result.answer or ""
    text = report_text + "\n" + answer_text
    checks = []
    for check in task.get("checks", []):
        ctype = check["type"]
        values = check.get("values") or []
        passed = False
        detail = ""
        if ctype == "status":
            passed = result.status.value in values
            detail = f"status={result.status.value}"
        elif ctype == "contains_any":
            hits = [v for v in values if v in text]
            passed = bool(hits)
            detail = f"命中={hits}" if hits else f"未命中任何 {values}"
        elif ctype == "contains_all":
            missing = [v for v in values if v not in text]
            passed = not missing
            detail = "全部命中" if passed else f"缺少={missing}"
        elif ctype == "report_has_findings":
            findings = (result.report or {}).get("findings") or []
            passed = bool(findings) and all(f.get("evidence") for f in findings)
            detail = f"findings={len(findings)}"
        elif ctype == "db_unchanged":
            passed = all(row_count(t) == before_counts[t] for t in values)
            detail = ", ".join(f"{t}:{before_counts[t]}->{row_count(t)}" for t in values)
        else:
            detail = f"未知检查类型 {ctype}"
        checks.append({"id": check["id"], "desc": check["desc"], "passed": passed, "detail": detail})
    return {
        "task_id": task["id"],
        "title": task["title"],
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "status": result.status.value,
        "steps": result.budget.get("steps"),
        "tool_calls": result.budget.get("tool_calls"),
        "tokens": result.usage.total_tokens,
        "duration_ms": result.duration_ms,
        "run_id": result.run_id,
        "report_title": (result.report or {}).get("title"),
        "answer": answer_text[:400],
    }


async def run_one(task: dict[str, Any], settings) -> dict[str, Any]:
    before = {t: row_count(t) for t in ("production_record", "energy_record", "workshop")}
    llm = build_llm(settings)
    loop = AgentLoop(llm, settings=settings)
    started = time.perf_counter()
    try:
        result = await loop.run(task["question"])
    finally:
        await llm.aclose()
    record = evaluate(task, result, before)
    record["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return record


def to_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "| 任务 | 结果 | 状态 | 步数 | 工具 | tokens | 耗时(s) | 未通过检查 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        failed = [c["desc"] for c in r["checks"] if not c["passed"]]
        lines.append(
            f"| {r['task_id']} | {'通过' if r['passed'] else '失败'} | {r['status']} | {r['steps']} | "
            f"{r['tool_calls']} | {r['tokens']} | {r['duration_ms'] / 1000:.1f} | "
            f"{'；'.join(failed) if failed else '—'} |"
        )
    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    lines.append("")
    lines.append(f"通过率：{passed}/{total}")
    return "\n".join(lines)


async def main(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only]
    if args.limit:
        tasks = tasks[: args.limit]

    if args.dry_run:
        for t in tasks:
            print(f"- {t['id']}: {t['question'][:60]}...")
            for c in t["checks"]:
                print(f"    [{c['type']}] {c['desc']}")
        return 0

    settings = get_settings()
    if not settings.has_llm:
        print("未配置 OPSPILOT_LLM_API_KEY，无法运行真实评测", file=sys.stderr)
        return 2

    records = []
    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] {task['id']} — {task['title']}")
        record = await run_one(task, settings)
        records.append(record)
        for c in record["checks"]:
            print(f"   {'PASS' if c['passed'] else 'FAIL'}  {c['desc']}  ({c['detail']})")
        print(f"   status={record['status']} steps={record['steps']} tokens={record['tokens']}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"eval-{stamp}.json"
    payload = {
        "model": settings.llm_model,
        "created_at": stamp,
        "records": records,
        "pass_rate": sum(1 for r in records if r["passed"]) / max(len(records), 1),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = to_markdown(records)
    (RESULTS_DIR / f"eval-{stamp}.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\n结果已写入 {out}")
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(description="OpsPilot 验收评测")
    parser.add_argument("--only", nargs="*", help="只跑指定任务 id")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
