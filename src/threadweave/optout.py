# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Opt-out registry — the "camera sign" layer.

Users can decline to have their content harvested. Every connector
(email daemon, SharePoint/OneNote daemon, Teams bot, ingest API) checks
this registry before storing knowledge attributed to a person.

Storage is a JSON file (default ~/.threadweave/optout.json) so the API
server and the daemons on the same host share one source of truth.

Keys are normalized email addresses / person IDs (lowercased).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_OPTOUT_FILE = "~/.threadweave/optout.json"


class OptOutStore:
    """Persistent per-person opt-out registry."""

    def __init__(self, path: str = DEFAULT_OPTOUT_FILE):
        self.path = os.path.expanduser(path)
        self._lock = threading.Lock()
        self._opted_out: set[str] = set()
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                data = json.loads(Path(self.path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw = data.get("opted_out", [])
                    self._opted_out = {
                        self._normalize(k) for k in raw if k
                    }
        except Exception as e:
            logger.warning("Failed to load opt-out registry %s: %s", self.path, e)

    def _save(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.path).write_text(
                json.dumps(
                    {"opted_out": sorted(self._opted_out)}, indent=2
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save opt-out registry: %s", e)

    # ---- API ----

    @staticmethod
    def _normalize(person: str) -> str:
        return (person or "").strip().lower()

    def opt_out(self, person: str) -> bool:
        """Register a person as opted out. Returns True if newly added."""
        key = self._normalize(person)
        if not key:
            return False
        with self._lock:
            if key in self._opted_out:
                return False
            self._opted_out.add(key)
            self._save()
        logger.info("Opt-out registered: %s", key)
        return True

    def opt_in(self, person: str) -> bool:
        """Remove a person from the opt-out registry."""
        key = self._normalize(person)
        with self._lock:
            if key not in self._opted_out:
                return False
            self._opted_out.discard(key)
            self._save()
        logger.info("Opt-out removed: %s", key)
        return True

    def is_opted_out(self, person: str) -> bool:
        """Check whether a person has opted out of harvesting."""
        return self._normalize(person) in self._opted_out

    def any_opted_out(self, persons: list[str]) -> bool:
        """True if ANY of the given persons is opted out."""
        return any(self.is_opted_out(p) for p in persons if p)

    def list_opted_out(self) -> list[str]:
        """All opted-out identities (sorted)."""
        with self._lock:
            return sorted(self._opted_out)


# Module-level singleton so the API server and connectors share state
_store: OptOutStore | None = None


def get_optout_store() -> OptOutStore:
    """Get the process-wide opt-out store (lazy singleton)."""
    global _store
    if _store is None:
        _store = OptOutStore()
    return _store
