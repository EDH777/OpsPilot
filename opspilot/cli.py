"""命令行入口：跑一次调查并打印过程。

    python -m opspilot.cli "三号车间近30天单位产品电耗为什么升高？"
    python -m opspilot.cli --mock        # 无 API Key 的离线演示
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from opspilot.config import get_settings
from opspilot.core.loop import AgentLoop
from opspilot.demo_scripts import mock_error_recovery, mock_for, mock_investigation
from opspilot.llm import build_llm
from opspilot.llm.base import LLMError

DEFAULT_TASK = "最近一个月，三号车间的单位产品电耗明显增加，但产量没有明显变化。请调查原因，并给出证据和改进建议。"


def _print_event(event) -> None:
    t, d = event.type, event.data
    if t == "run_started":
        print(f"[run] {event.run_id}  模型={d.get('model')}  工具={len(d.get('tools') or [])}")
    elif t == "llm_request":
        print(f"\n[step {d.get('step')}] 调用模型（上下文 {d.get('messages')} 条 / ~{d.get('est_tokens')} tokens）")
    elif t == "assistant_message":
        print(f"  助手: {d.get('content')}")
    elif t == "tool_started":
        args = (d.get("arguments") or "").replace("\n", " ")
        print(f"  -> {d.get('name')} {args[:160]}")
    elif t == "arg_repaired":
        print(f"     [harness] 自动修复参数: {'; '.join(d.get('repairs') or [])}")
    elif t == "tool_retry":
        print(f"     [harness] 重试 {d.get('name')} 第 {d.get('attempt')} 次（{d.get('kind')}）")
    elif t == "tool_finished":
        flag = "OK" if d.get("ok") else "FAIL"
        err = d.get("error") or {}
        suffix = f"  {err.get('kind')}: {err.get('message')[:80]}" if err else ""
        print(f"  <- {flag} {d.get('name')} {d.get('latency_ms')}ms{suffix}")
    elif t == "context_compacted":
        if d.get("compacted"):
            print(f"     [harness] 上下文压缩: 省略 {d.get('dropped_messages')} 条消息")
    elif t == "loop_guard":
        print(f"     [harness] 熔断: {d.get('name')} 连续失败，已提示模型换路径")
    elif t == "budget_exceeded":
        print(f"  !! 预算超限: {d.get('message')}")
    elif t == "error":
        print(f"  !! 错误: {d.get('message')}")
    elif t == "run_finished":
        print(
            f"\n[done] 状态={d.get('status')}  步数={d.get('budget', {}).get('steps')}  "
            f"工具={d.get('budget', {}).get('tool_calls')}  tokens={d.get('usage', {}).get('total_tokens')}  "
            f"耗时={d.get('duration_ms')}ms"
        )


def _print_report(result) -> None:
    report = result.report
    print("\n" + "=" * 72)
    if report:
        print(f"报告: {report.get('title')}")
        print(f"\n摘要:\n  {report.get('summary')}")
        metrics = report.get("metrics") or []
        if metrics:
            print("\n关键指标:")
            for m in metrics:
                print(f"  - {m.get('name')}: {m.get('value')} {m.get('unit')}  ({m.get('period')})")
        print("\n结论与证据:")
        for i, f in enumerate(report.get("findings") or [], 1):
            print(f"  {i}. [{f.get('confidence')}] {f.get('statement')}")
            print(f"     依据: {f.get('evidence')}")
        if report.get("recommendations"):
            print("\n建议:")
            for r in report["recommendations"]:
                print(f"  - {r}")
        if report.get("limitations"):
            print("\n局限:")
            for r in report["limitations"]:
                print(f"  - {r}")
    else:
        print("最终回答:")
        print(result.answer or "(无)")
    if result.error:
        print(f"\n错误: {result.error}")
    print(f"\nTrace: {result.trace_path}")
    print("=" * 72)


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    task = args.task or DEFAULT_TASK
    if args.demo == "recovery":
        llm = mock_error_recovery()
    elif args.demo == "investigation":
        llm = mock_investigation()
    elif args.mock:
        llm = mock_for(task)
    else:
        try:
            llm = build_llm(settings)
        except LLMError as exc:
            print(f"无法初始化模型：{exc}", file=sys.stderr)
            return 2

    loop = AgentLoop(llm, settings=settings)
    try:
        result = await loop.run(task, emit=_print_event)
    finally:
        await llm.aclose()

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(result)
    return 0 if result.status.value in {"completed"} else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="OpsPilot CLI")
    parser.add_argument("task", nargs="?", default=None, help="自然语言调查任务")
    parser.add_argument("--mock", action="store_true", help="使用离线脚本模型（无需 API Key）")
    parser.add_argument(
        "--demo",
        choices=["investigation", "recovery"],
        help="离线演示场景：investigation=完整调查，recovery=工具报错与恢复",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
