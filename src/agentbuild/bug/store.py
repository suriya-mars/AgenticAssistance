"""Per-bug state in SQLite: the bug record, completed steps, a timeline, and the bug's own mails."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    bug_id TEXT PRIMARY KEY, title TEXT, data_json TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS bug_steps (
    bug_id TEXT, step TEXT, done_at REAL, detail_json TEXT, PRIMARY KEY (bug_id, step)
);
CREATE TABLE IF NOT EXISTS bug_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bug_id TEXT, ts REAL, kind TEXT, step TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS bug_mails (
    bug_id TEXT, message_id TEXT, uid TEXT, direction TEXT, sender TEXT, recipients TEXT,
    subject TEXT, date TEXT, body TEXT, role TEXT, kind TEXT, summary TEXT, status TEXT, ts REAL,
    round INTEGER DEFAULT 1, PRIMARY KEY (bug_id, message_id)
);
"""


@dataclass
class Bug:
    bug_id: str
    title: str
    data: dict[str, Any]
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)   # step -> {done_at, detail}

    @property
    def done(self) -> set[str]:
        return set(self.steps)


class BugStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_mails)")}
        if "round" not in cols:  # databases created before rounds existed
            conn.execute("ALTER TABLE bug_mails ADD COLUMN round INTEGER DEFAULT 1")
            conn.commit()

    # ------------------------------------------------------------------ bugs
    def create(self, bug_id: str, title: str, data: dict[str, Any]) -> Bug:
        now = time.time()
        self.conn.execute("INSERT INTO bugs VALUES (?,?,?,?,?)", (bug_id, title, json.dumps(data), now, now))
        self.conn.commit()
        self.event(bug_id, "created", None, title)
        return self.get(bug_id)

    def exists(self, bug_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM bugs WHERE bug_id=?", (bug_id,)).fetchone() is not None

    def get(self, bug_id: str) -> Bug:
        row = self.conn.execute("SELECT title, data_json FROM bugs WHERE bug_id=?", (bug_id,)).fetchone()
        if row is None:
            raise KeyError(f"{bug_id} not found. Create it with: agent bug new {bug_id} --title \"...\"")
        steps = {r[0]: {"done_at": r[1], "detail": json.loads(r[2] or "{}")} for r in self.conn.execute(
            "SELECT step, done_at, detail_json FROM bug_steps WHERE bug_id=?", (bug_id,))}
        return Bug(bug_id, row[0], json.loads(row[1] or "{}"), steps)

    def update(self, bug: Bug) -> None:
        self.conn.execute("UPDATE bugs SET title=?, data_json=?, updated_at=? WHERE bug_id=?",
                          (bug.title, json.dumps(bug.data, default=str), time.time(), bug.bug_id))
        self.conn.commit()

    def list(self) -> list[Bug]:
        return [self.get(r[0]) for r in self.conn.execute("SELECT bug_id FROM bugs ORDER BY updated_at DESC")]

    # ----------------------------------------------------------------- steps
    def complete(self, bug_id: str, step: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO bug_steps VALUES (?,?,?,?)",
                          (bug_id, step, time.time(), json.dumps(detail or {}, default=str)))
        self.conn.execute("UPDATE bugs SET updated_at=? WHERE bug_id=?", (time.time(), bug_id))
        self.conn.commit()
        self.event(bug_id, "step_done", step, json.dumps(detail or {}, default=str))

    def clear_steps(self, bug_id: str) -> None:
        self.conn.execute("DELETE FROM bug_steps WHERE bug_id=?", (bug_id,))
        self.conn.commit()

    def delete(self, bug_id: str) -> None:
        for table in ("bugs", "bug_steps", "bug_events", "bug_mails"):
            self.conn.execute(f"DELETE FROM {table} WHERE bug_id=?", (bug_id,))
        self.conn.commit()

    def reopen(self, bug_id: str, step: str, reason: str) -> None:
        self.conn.execute("DELETE FROM bug_steps WHERE bug_id=? AND step=?", (bug_id, step))
        self.conn.commit()
        self.event(bug_id, "step_reopened", step, reason)

    # ---------------------------------------------------------------- events
    def event(self, bug_id: str, kind: str, step: str | None, detail: str) -> None:
        self.conn.execute("INSERT INTO bug_events (bug_id, ts, kind, step, detail) VALUES (?,?,?,?,?)",
                          (bug_id, time.time(), kind, step, detail))
        self.conn.commit()

    def events(self, bug_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT ts, kind, step, detail FROM bug_events WHERE bug_id=? ORDER BY id", (bug_id,))
        return [dict(zip(("ts", "kind", "step", "detail"), r)) for r in rows]

    # ----------------------------------------------------------------- mails
    def has_mail(self, bug_id: str, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM bug_mails WHERE bug_id=? AND message_id=?",
                                 (bug_id, message_id)).fetchone() is not None

    def add_mail(self, bug_id: str, message_id: str, *, uid: str = "", direction: str, sender: str,
                 recipients: str = "", subject: str, date: str, body: str, role: str | None = None,
                 kind: str = "", summary: str = "", status: str = "", round: int = 1) -> None:
        self.conn.execute("INSERT OR REPLACE INTO bug_mails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (bug_id, message_id, uid, direction, sender, recipients, subject, date, body,
                           role, kind, summary, status, time.time(), round))
        self.conn.commit()

    def set_mail_status(self, bug_id: str, message_id: str, status: str) -> None:
        self.conn.execute("UPDATE bug_mails SET status=? WHERE bug_id=? AND message_id=?", (status, bug_id, message_id))
        self.conn.commit()

    def close_open_mails(self, bug_id: str, status: str) -> None:
        """At restart: replies still waiting (deferred) belong to the old round and must not apply later."""
        self.conn.execute("UPDATE bug_mails SET status=? WHERE bug_id=? AND direction='in' AND status='new'",
                          (status, bug_id))
        self.conn.commit()

    def mails(self, bug_id: str) -> list[dict[str, Any]]:
        cols = ("message_id", "uid", "direction", "sender", "recipients", "subject", "date", "body",
                "role", "kind", "summary", "status", "ts", "round")
        rows = self.conn.execute(f"SELECT {', '.join(cols)} FROM bug_mails WHERE bug_id=? ORDER BY ts", (bug_id,))
        return [dict(zip(cols, r)) for r in rows]
