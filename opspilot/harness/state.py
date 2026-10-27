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

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
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
    resume_count INTEGER DEFAULT 0,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at DESC);
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
        self._migrate()
        self._conn.commit()

    # ------------------------------------------------------------------ 迁移

    def _migrate(self) -> None:
        """轻量迁移：老库没有 schema_meta / 新列时自动补齐，不需要手工操作。"""
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        current = int(row[0]) if row else 0

        if current < 1:
            # 基础表由 _SCHEMA 的 IF NOT EXISTS 保证
            current = 1

        if current < 2:
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "resume_count" not in columns:
                self._conn.execute("ALTER TABLE runs ADD COLUMN resume_count INTEGER DEFAULT 0")
            current = 2

        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(min(current, _SCHEMA_VERSION)),),
        )

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        return int(row[0]) if row else 0

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
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def archive_session(self, session_id: str) -> dict[str, Any] | None:
        """删除对话，但保留产出过报告的运行。

        - 有报告的 run：脱离会话（session_id=NULL），清空 messages/answer，
          保留 task（产生报告的那句提问）、report、Trace 与产物目录；
        - 没有报告的 run：整行删除，由调用方清理对应目录。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, task, report FROM runs WHERE session_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
            if not rows:
                return None

            kept: list[dict[str, Any]] = []
            removed: list[str] = []
            for row in rows:
                has_report = bool(row["report"]) and row["report"] != "null"
                if has_report:
                    self._conn.execute(
                        "UPDATE runs SET session_id = NULL, messages = '[]', answer = NULL WHERE run_id = ?",
                        (row["run_id"],),
                    )
                    kept.append({"run_id": row["run_id"], "task": row["task"]})
                else:
                    self._conn.execute("DELETE FROM runs WHERE run_id = ?", (row["run_id"],))
                    removed.append(row["run_id"])
            self._conn.commit()
        return {"kept": kept, "removed": removed, "queries": [r["task"] for r in rows if r["task"]]}

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, session_id, task, title, status, report, created_at, updated_at "
                "FROM runs WHERE report IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def mark_interrupted_runs(self) -> int:
        """服务启动时调用：把上次进程遗留的 running 标记为 interrupted（可续跑）。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET status = 'interrupted', "
                "error = COALESCE(NULLIF(error, ''), '服务重启导致运行中断，可继续运行') "
                "WHERE status = 'running'"
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def bump_resume(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET resume_count = COALESCE(resume_count, 0) + 1 WHERE run_id = ?",
                (run_id,),
            )
            self._conn.commit()

    def session_history(
        self,
        session_id: str,
        *,
        exclude_run_id: str | None = None,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """取同一会话已结束的轮次，供新 run 继承指代与约束。"""
        sql = (
            "SELECT run_id, task, answer, report, status, created_at FROM runs "
            "WHERE session_id = ? AND status IN ('completed', 'failed')"
        )
        params: list[Any] = [session_id]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [self._decode(dict(r)) for r in rows]
        out.reverse()
        return out

    def runs_since(self, since: float = 0.0, limit: int = 500) -> list[dict[str, Any]]:
        """按时间窗口取 run 元数据，供指标聚合使用（不取 messages/report 大字段）。"""
        sql = (
            "SELECT run_id, session_id, status, step, usage, created_at, updated_at "
            "FROM runs"
        )
        params: list[Any] = []
        if since:
            sql += " WHERE created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """会话维度：一个 session 下可能有多轮 run，侧栏应按会话展示。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.session_id,
                       (SELECT r.task FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at ASC, r.rowid ASC LIMIT 1) AS title,
                       COUNT(*) AS runs,
                       MIN(s.created_at) AS started_at,
                       MAX(s.created_at) AS last_at,
                       (SELECT r.status FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1) AS last_status,
                       (SELECT r.run_id FROM runs r WHERE r.session_id = s.session_id
                        ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1) AS last_run_id
                FROM runs s
                WHERE s.session_id IS NOT NULL
                GROUP BY s.session_id
                ORDER BY last_at DESC, s.session_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def session_runs(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, session_id, task, status, step, usage, answer, report, created_at "
                "FROM runs WHERE session_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
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
