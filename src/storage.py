"""SQLite state. Scope is part of every key; network operations are never retried here."""

import json
import sqlite3
import time
from pathlib import Path
from .memory_state import MemoryState


class Store:
    def __init__(self, path: Path, scope: str, budget_scope: str | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.scope = scope
        self.budget_scope = budget_scope or scope
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events(
                scope TEXT, id TEXT, status TEXT, PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS context(
                scope TEXT PRIMARY KEY, messages TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_clock(
                scope TEXT PRIMARY KEY, timestamps TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quote_routes(
                scope TEXT, message_id TEXT, created REAL, PRIMARY KEY(scope,message_id));
            CREATE TABLE IF NOT EXISTS usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, event_id TEXT,
                reserved REAL, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, status TEXT);
        """)
        with self.db:
            self.db.execute(
                "UPDATE events SET status='interrupted' WHERE scope=? AND status='accepted'",
                (scope,),
            )
            self.db.execute(
                "UPDATE events SET status='unknown' WHERE scope=? AND status='sending'",
                (scope,),
            )
        self.memory = MemoryState(self.db, scope)
        # Legacy messages have no timestamps: start their seven-day clock at migration.
        row = self.db.execute(
            "SELECT messages FROM context WHERE scope=?", (scope,)
        ).fetchone()
        if row:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO context_clock VALUES(?,?)",
                    (scope, json.dumps([time.time()] * len(json.loads(row[0])))),
                )

    def claim(self, event_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,'accepted')",
                (self.scope, event_id),
            )
        return result.rowcount == 1

    def note_route(self, message_id):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO quote_routes VALUES(?,?,?)",
                (self.scope, str(message_id), time.time()),
            )
            self.db.execute(
                "DELETE FROM quote_routes WHERE scope=? AND created<?",
                (self.scope, time.time() - 30 * 86400),
            )

    def known_route(self, message_id):
        return (
            self.db.execute(
                "SELECT 1 FROM quote_routes WHERE scope=? AND message_id=? AND created>?",
                (self.scope, str(message_id), time.time() - 30 * 86400),
            ).fetchone()
            is not None
        )

    def status(self, event_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE events SET status=? WHERE scope=? AND id=?",
                (status, self.scope, event_id),
            )

    def history(self) -> list[dict]:
        row = self.db.execute(
            "SELECT messages FROM context WHERE scope=?", (self.scope,)
        ).fetchone()
        messages = json.loads(row[0]) if row else []
        clock = self.db.execute(
            "SELECT timestamps FROM context_clock WHERE scope=?", (self.scope,)
        ).fetchone()
        stamps = json.loads(clock[0]) if clock else [time.time()] * len(messages)
        cutoff = time.time() - 7 * 86400
        kept = [
            (message, stamp)
            for message, stamp in zip(messages, stamps)
            if stamp >= cutoff
        ]
        if len(kept) != len(messages):
            self._write_history([m for m, _ in kept], [s for _, s in kept])
        return [m for m, _ in kept]

    def save_history(self, messages: list[dict]):
        previous = self.history()
        clock = self.db.execute(
            "SELECT timestamps FROM context_clock WHERE scope=?", (self.scope,)
        ).fetchone()
        old_stamps = json.loads(clock[0]) if clock else [time.time()] * len(previous)
        overlap = min(len(previous), len(messages))
        while overlap and previous[-overlap:] != messages[:overlap]:
            overlap -= 1
        stamps = (old_stamps[-overlap:] if overlap else []) + [time.time()] * (
            len(messages) - overlap
        )
        self._write_history(messages, stamps)

    def _write_history(self, messages, stamps):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO context VALUES(?,?)",
                (self.scope, json.dumps(messages, ensure_ascii=False)),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO context_clock VALUES(?,?)",
                (self.scope, json.dumps(stamps)),
            )

    def memories(self) -> list[tuple[int, str]]:
        return self.db.execute(
            "SELECT id,text FROM memories WHERE scope=? ORDER BY id", (self.scope,)
        ).fetchall()

    def remember(self, text: str) -> int:
        with self.db:
            result = self.db.execute(
                "INSERT INTO memories(scope,text) VALUES(?,?)", (self.scope, text)
            )
        return result.lastrowid

    def forget(self, memory_id: int) -> bool:
        with self.db:
            result = self.db.execute(
                "DELETE FROM memories WHERE scope=? AND id=?", (self.scope, memory_id)
            )
        return result.rowcount == 1

    def usage(self) -> tuple[int, float]:
        count, reserved = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM usage WHERE scope=?",
            (self.budget_scope,),
        ).fetchone()
        return count, reserved

    def reserve(
        self,
        event_id: str,
        amount: float,
        budget: float | None,
        max_requests: int | None,
    ) -> int | None:
        # Reserved amounts are never refunded: timeout/cancellation may still be billed.
        with self.db:
            count, spent = self.usage()
            if (max_requests is not None and count >= max_requests) or (
                budget is not None and spent + amount > budget
            ):
                return None
            result = self.db.execute(
                "INSERT INTO usage(scope,event_id,reserved,status) VALUES(?,?,?,'reserved')",
                (self.budget_scope, event_id, amount),
            )
        return result.lastrowid

    def record_usage(self, usage_id: int, status: str, input_tokens=0, output_tokens=0):
        with self.db:
            self.db.execute(
                "UPDATE usage SET status=?,input_tokens=?,output_tokens=? WHERE id=? AND scope=?",
                (status, input_tokens, output_tokens, usage_id, self.budget_scope),
            )

    def close(self):
        self.db.close()
