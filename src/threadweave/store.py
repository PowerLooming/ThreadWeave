# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Durable entry store — SQLite persistence for the knowledge palace.

The API keeps entries in memory for fast search and lookup, but that
meant a restart wiped the palace. This store is the write-through
persistence layer: every save/delete is mirrored to SQLite, and the
store is reloaded into memory at startup.

Schema mirrors the entry dict exactly (JSON columns for nested fields),
so the in-memory dicts remain the single source of truth during a run
and the DB is only the durability mirror.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "~/.threadweave/entries.sqlite3"

_ENTRY_COLUMNS = (
    "id", "content", "wing", "room", "scope", "source_type",
    "author_id", "title", "created_at", "entities", "content_type",
    "has_pii", "tenant_id", "source_metadata", "sensitivity",
    "client_id", "allowed_people",
)

_SCHEMA_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    wing TEXT DEFAULT '',
    room TEXT DEFAULT 'general',
    scope TEXT DEFAULT 'team',
    source_type TEXT DEFAULT 'manual',
    author_id TEXT DEFAULT 'unknown',
    title TEXT DEFAULT '',
    created_at TEXT,
    entities TEXT DEFAULT '[]',
    content_type TEXT DEFAULT 'answer',
    has_pii INTEGER DEFAULT 0,
    tenant_id TEXT DEFAULT 'default',
    source_metadata TEXT DEFAULT '{}',
    sensitivity TEXT DEFAULT 'internal',
    client_id TEXT,
    allowed_people TEXT DEFAULT '[]'
)
""",
    "CREATE INDEX IF NOT EXISTS idx_entries_tenant ON entries(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_entries_wing ON entries(wing)",
]


class EntryStore:
    """SQLite-backed persistence for knowledge entries (write-through)."""

    def __init__(self, db_path: str | None = None):
        path = db_path or os.environ.get(
            "THREADWEAVE_ENTRY_DB", DEFAULT_DB_PATH
        )
        self.path = os.path.expanduser(path)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
            self._db = conn
        except Exception as exc:
            logger.warning(
                "Entry DB unavailable at %s (%s) — entries will be "
                "memory-only for this run", self.path, exc,
            )
            self._db = None

    # ---- serialization ----

    @staticmethod
    def _to_row(entry: dict) -> tuple:
        return (
            entry.get("id", ""),
            entry.get("content", ""),
            entry.get("wing", ""),
            entry.get("room", "general"),
            entry.get("scope", "team"),
            entry.get("source_type", "manual"),
            entry.get("author_id", "unknown"),
            entry.get("title", ""),
            entry.get("created_at", ""),
            json.dumps(entry.get("entities", [])),
            entry.get("content_type", "answer"),
            1 if entry.get("has_pii") else 0,
            entry.get("tenant_id", "default"),
            json.dumps(entry.get("source_metadata", {})),
            entry.get("sensitivity", "internal"),
            entry.get("client_id"),
            json.dumps(entry.get("allowed_people", [])),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "content": row["content"],
            "wing": row["wing"],
            "room": row["room"],
            "scope": row["scope"],
            "source_type": row["source_type"],
            "author_id": row["author_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "entities": json.loads(row["entities"] or "[]"),
            "content_type": row["content_type"],
            "has_pii": bool(row["has_pii"]),
            "tenant_id": row["tenant_id"],
            "source_metadata": json.loads(row["source_metadata"] or "{}"),
            "sensitivity": row["sensitivity"],
            "client_id": row["client_id"],
            "allowed_people": json.loads(row["allowed_people"] or "[]"),
        }

    # ---- writes ----

    def save(self, entry: dict) -> None:
        """Upsert an entry (write-through)."""
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    f"INSERT OR REPLACE INTO entries ({','.join(_ENTRY_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(_ENTRY_COLUMNS))})",
                    self._to_row(entry),
                )
                self._db.commit()
        except Exception as exc:
            logger.warning("Entry save failed for %s: %s", entry.get("id"), exc)

    def delete(self, entry_id: str) -> None:
        """Delete an entry by ID (write-through)."""
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM entries WHERE id = ?", (entry_id,)
                )
                self._db.commit()
        except Exception as exc:
            logger.warning("Entry delete failed for %s: %s", entry_id, exc)

    # ---- reads ----

    def load_all(self) -> list[dict]:
        """Load every entry (called at startup to rebuild the memory store)."""
        if self._db is None:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT * FROM entries ORDER BY created_at"
                ).fetchall()
            return [self._from_row(r) for r in rows]
        except Exception as exc:
            logger.warning("Entry load failed: %s", exc)
            return []

    def get(self, entry_id: str) -> dict | None:
        if self._db is None:
            return None
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT * FROM entries WHERE id = ?", (entry_id,)
                ).fetchone()
            return self._from_row(row) if row else None
        except Exception as exc:
            logger.warning("Entry get failed for %s: %s", entry_id, exc)
            return None

    def count(self) -> int:
        if self._db is None:
            return 0
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM entries"
                ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0


# Process-wide singleton (mirrors the audit log pattern)
_store: EntryStore | None = None


def get_entry_store() -> EntryStore:
    """Get the process-wide entry store (lazy singleton)."""
    global _store
    if _store is None:
        _store = EntryStore()
    return _store
