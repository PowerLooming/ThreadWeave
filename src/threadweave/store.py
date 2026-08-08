# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Durable entry store — persistence for the knowledge palace.

The API keeps entries in memory for fast search and lookup, but that
meant a restart wiped the palace. This store is the write-through
persistence layer: every save/delete is mirrored to the database, and
the store is reloaded into memory at startup.

Backends (SQLAlchemy Core — one code path, dialect handles the rest):
- SQLite (default):  sqlite:///~/.threadweave/entries.sqlite3
- PostgreSQL:        postgresql://user:pass@host:5432/dbname

Configure with THREADWEAVE_ENTRY_DB (URL). The in-memory dicts remain
the single source of truth during a run; the DB is the durability
mirror and the reload source at startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_URL = "sqlite:///~/.threadweave/entries.sqlite3"


def _entry_url() -> str:
    """Resolve the entry DB URL from the environment."""
    url = os.environ.get("THREADWEAVE_ENTRY_DB", DEFAULT_DB_URL)
    # Accept a bare path (legacy) as SQLite
    if not url.startswith(("sqlite:", "postgresql:", "postgres:")):
        url = f"sqlite:///{url}"
    # SQLAlchemy does NOT expand ~ in SQLite paths — do it here so the
    # default points at the real home dir (regression caught live:
    # sqlite:///~/.threadweave/... opened a literal-~ file instead of
    # the existing entries.sqlite3).
    if url.startswith("sqlite:///") and "~" in url:
        path = url[len("sqlite:///"):]
        url = f"sqlite:///{os.path.expanduser(path)}"
    return url


class EntryStore:
    """SQLAlchemy-backed persistence for knowledge entries (write-through).

    Supports SQLite and PostgreSQL via the connection URL. The schema
    mirrors the entry dict exactly (JSON columns for nested fields).
    """

    def __init__(self, url: str | None = None, table_name: str = "entries"):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        self.url = url or _entry_url()
        self.table_name = table_name
        # NullPool: the API is a long-lived single process; pooled
        # connections add nothing here and complicate multi-backend use.
        self._engine = create_engine(self.url, poolclass=NullPool)
        self._lock = threading.Lock()
        self._init_db()

    # ---- schema ----

    def _init_db(self) -> None:
        from sqlalchemy import text

        statements = [
            f"""
CREATE TABLE IF NOT EXISTS {self.table_name} (
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
    source_metadata TEXT DEFAULT '{{}}',
    sensitivity TEXT DEFAULT 'internal',
    client_id TEXT,
    allowed_people TEXT DEFAULT '[]',
    version_of TEXT
)
""",
            f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_tenant "
            f"ON {self.table_name}(tenant_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_wing "
            f"ON {self.table_name}(wing)",
        ]
        try:
            with self._engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
                # Migration for existing DBs: add version_of if missing.
                has_version = any(
                    col["name"] == "version_of"
                    for col in self._columns()
                )
                if not has_version:
                    conn.execute(text(
                        f"ALTER TABLE {self.table_name} ADD COLUMN "
                        "version_of TEXT"
                    ))
        except Exception as exc:
            logger.warning(
                "Entry DB unavailable at %s (%s) — entries will be "
                "memory-only for this run", self.url, exc,
            )

    def _columns(self) -> list[dict]:
        """Column metadata for the current table (dialect-neutral)."""
        from sqlalchemy import inspect

        try:
            return inspect(self._engine).get_columns(self.table_name)
        except Exception:
            return []

    # ---- serialization ----

    @staticmethod
    def _to_row(entry: dict) -> dict:
        return {
            "id": entry.get("id", ""),
            "content": entry.get("content", ""),
            "wing": entry.get("wing", ""),
            "room": entry.get("room", "general"),
            "scope": entry.get("scope", "team"),
            "source_type": entry.get("source_type", "manual"),
            "author_id": entry.get("author_id", "unknown"),
            "title": entry.get("title", ""),
            "created_at": entry.get("created_at", ""),
            "entities": json.dumps(entry.get("entities", [])),
            "content_type": entry.get("content_type", "answer"),
            "has_pii": 1 if entry.get("has_pii") else 0,
            "tenant_id": entry.get("tenant_id", "default"),
            "source_metadata": json.dumps(entry.get("source_metadata", {})),
            "sensitivity": entry.get("sensitivity", "internal"),
            "client_id": entry.get("client_id"),
            "allowed_people": json.dumps(entry.get("allowed_people", [])),
            "version_of": entry.get("version_of"),
        }

    @staticmethod
    def _from_row(row) -> dict:
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
            "version_of": row["version_of"],
        }

    # ---- writes ----

    def save(self, entry: dict) -> None:
        """Upsert an entry (write-through)."""
        from sqlalchemy import text

        row = self._to_row(entry)
        cols = list(row.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(cols)}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(text(sql), row)
        except Exception as exc:
            logger.warning("Entry save failed for %s: %s", entry.get("id"), exc)

    def delete(self, entry_id: str) -> None:
        """Delete an entry by ID (write-through)."""
        from sqlalchemy import text

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f"DELETE FROM {self.table_name} WHERE id = :id"),
                    {"id": entry_id},
                )
        except Exception as exc:
            logger.warning("Entry delete failed for %s: %s", entry_id, exc)

    # ---- reads ----

    def load_all(self) -> list[dict]:
        """Load every entry (called at startup to rebuild the memory store)."""
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(f"SELECT * FROM {self.table_name} ORDER BY created_at")
                ).mappings().all()
            return [self._from_row(r) for r in rows]
        except Exception as exc:
            logger.warning("Entry load failed: %s", exc)
            return []

    def get(self, entry_id: str) -> dict | None:
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(f"SELECT * FROM {self.table_name} WHERE id = :id"),
                    {"id": entry_id},
                ).mappings().first()
            return self._from_row(row) if row else None
        except Exception as exc:
            logger.warning("Entry get failed for %s: %s", entry_id, exc)
            return None

    def count(self) -> int:
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(f"SELECT COUNT(*) AS n FROM {self.table_name}")
                ).mappings().first()
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    # ---- versioning ----

    def find_by_source_key(self, source_key: str) -> list[dict]:
        """Entries whose source_metadata matches a source identity key.

        Used for version chaining: a re-captured document (same
        source_file) becomes a new version of the earlier capture.
        JSON filtering is done in Python for dialect neutrality.
        """
        if not source_key:
            return []
        matches = []
        for entry in self.load_all():
            meta = entry.get("source_metadata") or {}
            if meta.get("source_file") == source_key:
                matches.append(entry)
        matches.sort(key=lambda e: e.get("created_at", ""))
        return matches


# Process-wide singleton (mirrors the audit log pattern)
_store: EntryStore | None = None


def get_entry_store() -> EntryStore:
    """Get the process-wide entry store (lazy singleton)."""
    global _store
    if _store is None:
        _store = EntryStore()
    return _store
