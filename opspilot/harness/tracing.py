"""运行追踪：每个 run 一个目录，事件逐行落盘，支持回放与 UI 轮询。

JSONL 而不是数据库：便于 grep、便于直接贴给面试官看、写入失败也不影响主流程。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from opspilot.core.events import Event


class Tracer:
    def __init__(self, run_id: str, runs_dir: Path) -> None:
        self.run_id = run_id
        self.root = Path(runs_dir) / run_id
        self.artifacts_dir = self.root / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.root / "trace.jsonl"
        self._lock = threading.Lock()

    def emit(self, event: Event) -> None:
        try:
            line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"seq": event.seq, "type": event.type, "run_id": event.run_id, "data": {"unserializable": True}}, ensure_ascii=False)
        with self._lock:
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    # ---- 静态读取（UI / 回放）----

    @staticmethod
    def load_events(run_id: str, runs_dir: Path) -> list[dict]:
        path = Path(runs_dir) / run_id / "trace.jsonl"
        if not path.exists():
            return []
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
