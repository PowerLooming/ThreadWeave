# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Capture notifications — the "camera sign" that talks.

When the daemons save knowledge from a person's content (email,
SharePoint, OneNote), a notification is queued so the Teams bot can
DM that person: "your email about X was added to the palace".

Privacy contract:
- Notifications are queued ONLY for the content AUTHOR, never for
  unrelated people.
- Opted-out people never generate entries, so they never generate
  notifications (the ingest opt-out gate runs before the queue).
- Notifications are marked delivered once sent; the queue is durable
  (SQLite) so restarts don't lose or duplicate notifications.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "~/.threadweave/notifications.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    wing TEXT DEFAULT '',
    room TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at TEXT,
    delivered INTEGER DEFAULT 0
)
"""


class NotificationStore:
    """Durable queue of capture notifications awaiting delivery."""

    def __init__(self, db_path: str | None = None):
        path = db_path or os.environ.get(
            "THREADWEAVE_NOTIFY_DB", DEFAULT_DB_PATH
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
            conn.execute(_SCHEMA)
            conn.commit()
            self._db = conn
        except Exception as exc:
            logger.warning(
                "Notification DB unavailable at %s (%s) — notifications "
                "will be memory-only", self.path, exc,
            )
            self._db = None

    def enqueue(
        self,
        notification_id: str,
        entry_id: str,
        author_id: str,
        title: str = "",
        wing: str = "",
        room: str = "",
        source: str = "",
        created_at: str = "",
    ) -> bool:
        """Queue a notification. Returns False if already queued."""
        if self._db is None:
            return False
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO notifications "
                    "(id, entry_id, author_id, title, wing, room, source, "
                    " created_at, delivered) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (notification_id, entry_id, author_id, title, wing,
                     room, source, created_at),
                )
                self._db.commit()
            return cur.rowcount > 0
        except Exception as exc:
            logger.warning("Notify enqueue failed: %s", exc)
            return False

    def pending(self, limit: int = 50) -> list[dict]:
        if self._db is None:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT * FROM notifications WHERE delivered = 0 "
                    "ORDER BY created_at LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Notify pending failed: %s", exc)
            return []

    def mark_delivered(self, notification_id: str, skipped: bool = False) -> None:
        """Mark delivered (1) or undeliverable/skipped (2)."""
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE notifications SET delivered = ? WHERE id = ?",
                    (2 if skipped else 1, notification_id),
                )
                self._db.commit()
        except Exception as exc:
            logger.warning("Notify delivered failed: %s", exc)

    def count(self, delivered_only: bool = False) -> int:
        if self._db is None:
            return 0
        try:
            with self._lock:
                if delivered_only:
                    row = self._db.execute(
                        "SELECT COUNT(*) AS n FROM notifications "
                        "WHERE delivered = 1"
                    ).fetchone()
                else:
                    row = self._db.execute(
                        "SELECT COUNT(*) AS n FROM notifications "
                        "WHERE delivered = 0"
                    ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    def count_skipped(self) -> int:
        """Notifications marked undeliverable (delivered = 2)."""
        if self._db is None:
            return 0
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM notifications "
                    "WHERE delivered = 2"
                ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0


# Process-wide singleton
_store: NotificationStore | None = None


def get_notification_store() -> NotificationStore:
    global _store
    if _store is None:
        _store = NotificationStore()
    return _store
