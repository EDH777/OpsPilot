"""状态持久化：SQLite 检查点，支持暂停/崩溃后从最后一步继续。

只存"恢复所需的最小状态"：消息序列、步数、预算用量、终态产物。
Trace 负责过程细节，State 负责可续跑。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    session_id  TEXT,
    task        TEXT,
    title       TEXT,
    status      TEXT,
    step        INTEGER DEFAULT 0,
    messages    TEXT,
    usage       TEXT,
    answer      TEXT,
    report      TEXT,
    error       TEXT,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at DESC);

-- 删除对话时只保留用户提问，作为轻量归档
CREATE TABLE IF NOT EXISTS deleted_sessions (
    session_id  TEXT PRIMARY KEY,
    first_query TEXT,
    queries     TEXT,
    run_count   INTEGER,
    deleted_at  REAL
);
"""

_JSON_FIELDS = {"messages", "usage", "report"}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ 写

    def save(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "session_id",
            "task",
            "title",
            "status",
            "step",
            "messages",
            "usage",
            "answer",
            "report",
            "error",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        for key in _JSON_FIELDS & data.keys():
            data[key] = json.dumps(data[key], ensure_ascii=False, default=str) if data[key] is not None else None

        now = time.time()
        with self._lock:
            existing = self._conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if existing:
                data["updated_at"] = now
                cols = ", ".join(f"{k} = ?" for k in data)
                self._conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", (*data.values(), run_id))
            else:
                row = {"run_id": run_id, "created_at": now, "updated_at": now, **data}
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                self._conn.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", tuple(row.values()))
            self._conn.commit()

    def delete(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ 读

    def load(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return self._decode(dict(row))

    def list_runs(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT run_id, session_id, task, title, status, step, answer, created_at, updated_at FROM runs"
        params: list[Any] = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> dict[str, Any] | None:
        """删除一个会话的全部 run，但把用户提问归档保留下来。

        返回 run_ids / queries 供调用方清理 Trace 与产物目录。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, task FROM runs WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            if not rows:
                return None
            queries = [r["task"] for r in rows if r["task"]]
            run_ids = [r["run_id"] for r in rows]
            self._conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
            self._conn.execute(
                "INSERT OR REPLACE INTO deleted_sessions (session_id, first_query, queries, run_count, deleted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    queries[0] if queries else "",
                    json.dumps(queries, ensure_ascii=False),
                    len(run_ids),
                    time.time(),
                ),
            )
            self._conn.commit()
        return {"session_id": session_id, "run_ids": run_ids, "queries": queries, "run_count": len(run_ids)}

    def list_deleted_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, first_query, queries, run_count, deleted_at "
                "FROM deleted_sessions ORDER BY deleted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["queries"] = json.loads(item.get("queries") or "[]")
            except json.JSONDecodeError:
                item["queries"] = []
            out.append(item)
        return out

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, session_id, task, title, status, report, created_at, updated_at "
                "FROM runs WHERE report IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """会话维度：一个 session 下可能有多轮 run，侧栏应按会话展示。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.session_id,
                       (SELECT r.task FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at ASC LIMIT 1) AS title,
                       COUNT(*) AS runs,
                       MIN(s.created_at) AS started_at,
                       MAX(s.created_at) AS last_at,
                       (SELECT r.status FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at DESC LIMIT 1) AS last_status,
                       (SELECT r.run_id FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at DESC LIMIT 1) AS last_run_id
                FROM runs s
                WHERE s.session_id IS NOT NULL
                GROUP BY s.session_id
                ORDER BY last_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def session_runs(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, session_id, task, status, step, usage, answer, report, created_at "
                "FROM runs WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_id, MAX(created_at) AS last_at, COUNT(*) AS runs
                FROM runs WHERE session_id IS NOT NULL
                GROUP BY session_id ORDER BY last_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for key in _JSON_FIELDS:
            value = row.get(key)
            if isinstance(value, str) and value:
                try:
                    row[key] = json.loads(value)
                except json.JSONDecodeError:
                    row[key] = None
        return row
