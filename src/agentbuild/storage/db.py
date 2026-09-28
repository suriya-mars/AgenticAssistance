"""SQLite store: processed messages, tasks, runs, approvals."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY, uid TEXT, sender TEXT, subject TEXT,
    triage_json TEXT, processed_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_message_id TEXT, title TEXT,
    due TEXT, owner TEXT, status TEXT DEFAULT 'open', created_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, agent TEXT, trigger TEXT, status TEXT,
    result_json TEXT, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, action TEXT,
    detail TEXT, approved INTEGER, decided_at REAL
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def is_processed(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def mark_processed(self, message_id: str, uid: str, sender: str, subject: str, triage: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_messages VALUES (?,?,?,?,?,?)",
            (message_id, uid, sender, subject, json.dumps(triage), time.time()),
        )
        self.conn.commit()

    def add_task(self, title: str, source_message_id: str = "", due: str = "", owner: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks (source_message_id,title,due,owner,created_at) VALUES (?,?,?,?,?)",
            (source_message_id, title, due, owner, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_tasks(self, status: str = "open") -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id", (status,))
        return [dict(r) for r in rows]

    def record_run(self, run_id: str, agent: str, trigger: str, status: str, result: Any, started: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
            (run_id, agent, trigger, status, json.dumps(result, default=str), started, time.time()),
        )
        self.conn.commit()

    def record_approval(self, run_id: str, action: str, detail: str, approved: bool) -> None:
        self.conn.execute(
            "INSERT INTO approvals (run_id,action,detail,approved,decided_at) VALUES (?,?,?,?,?)",
            (run_id, action, detail, int(approved), time.time()),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
