"""追加写 JSONL 审计。

轻量版只保证“有记录、可追查”，不做哈希链。记录里不放密码、token、
SQL 字面量、对话正文等敏感原值，只放动作、决策、原因和 run_id。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, runs_dir: Path) -> None:
        self.path = Path(runs_dir) / "audit.jsonl"
        self._lock = threading.Lock()

    def record(
        self,
        *,
        action: str,
        decision: str,
        user_id: str,
        role: str | None,
        run_id: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "user_id": user_id,
            "role": role,
            "action": action,
            "decision": decision,
            "reason": reason,
            "run_id": run_id,
            "detail": detail or {},
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
