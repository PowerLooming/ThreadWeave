# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Graph Sync Engine — orchestrates ongoing synchronization between ThreadWeave
and Microsoft Graph.

Runs as a scheduled daemon:
- On startup: performs a full sync
- Thereafter: incremental sync on a configurable interval
- Stores sync state (last sync timestamp) locally

Designed to run as a background process alongside ThreadWeave API.
Can also be triggered manually via CLI: `threadweave graph sync`
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from threadweave.connectors.graph.connector import (
    ThreadWeaveGraphConnector,
    SyncStats,
)

logger = logging.getLogger("threadweave.graph.sync")

# Default sync state file (persists last sync timestamp)
DEFAULT_STATE_FILE = os.path.expanduser("~/.threadweave/graph_sync_state.json")

# Default sync interval (seconds)
DEFAULT_SYNC_INTERVAL = 300  # 5 minutes


@dataclass
class SyncState:
    """Persisted state for incremental sync tracking."""
    last_full_sync: str = ""
    last_incremental_sync: str = ""
    items_synced: int = 0
    total_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "last_full_sync": self.last_full_sync,
            "last_incremental_sync": self.last_incremental_sync,
            "items_synced": self.items_synced,
            "total_failures": self.total_failures,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SyncState":
        return cls(
            last_full_sync=data.get("last_full_sync", ""),
            last_incremental_sync=data.get("last_incremental_sync", ""),
            items_synced=data.get("items_synced", 0),
            total_failures=data.get("total_failures", 0),
        )

    @classmethod
    def load(cls, path: str = DEFAULT_STATE_FILE) -> "SyncState":
        """Load sync state from disk."""
        try:
            with open(path, "r") as f:
                return cls.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

    def save(self, path: str = DEFAULT_STATE_FILE) -> None:
        """Persist sync state to disk."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class SyncEngine:
    """Orchestrates ThreadWeave → Microsoft Graph synchronization.

    Can run as a one-shot sync or as a continuous daemon.
    """

    def __init__(
        self,
        connector: ThreadWeaveGraphConnector,
        state_file: str = DEFAULT_STATE_FILE,
        sync_interval: int = DEFAULT_SYNC_INTERVAL,
    ):
        self.connector = connector
        self.state_file = state_file
        self.sync_interval = sync_interval
        self.state = SyncState.load(state_file)
        self._running = False
        self._last_stats: Optional[SyncStats] = None

    # ── One-Shot Sync ──────────────────────────────────────────────

    def full_sync(self) -> SyncStats:
        """Perform a full sync of all entries to Microsoft Graph."""
        logger.info("Starting full sync...")

        stats = self.connector.full_sync()

        # Update state
        now = datetime.now(timezone.utc).isoformat()
        self.state.last_full_sync = now
        self.state.last_incremental_sync = now
        self.state.items_synced = stats.created + stats.updated
        self.state.total_failures += stats.failed
        self.state.save(self.state_file)

        self._last_stats = stats
        return stats

    def incremental_sync(self) -> SyncStats:
        """Perform an incremental sync (entries changed since last sync)."""
        since = self.state.last_incremental_sync or None
        logger.info("Starting incremental sync (since %s)...", since)

        stats = self.connector.incremental_sync(since=since)

        # Update state
        now = datetime.now(timezone.utc).isoformat()
        self.state.last_incremental_sync = now
        self.state.items_synced += stats.created + stats.updated
        self.state.total_failures += stats.failed
        self.state.save(self.state_file)

        self._last_stats = stats
        return stats

    def schema_setup(self) -> bool:
        """One-time schema registration with Microsoft Graph."""
        logger.info("Registering Graph connection schema...")
        return self.connector.register_schema()

    # ── Daemon Mode ────────────────────────────────────────────────

    def run_daemon(self) -> None:
        """Run as a continuous sync daemon.

        Performs a full sync on startup, then incremental syncs
        on the configured interval. Handles SIGTERM/SIGINT for
        graceful shutdown.
        """
        if not self.connector.is_configured:
            logger.error(
                "Graph credentials not configured. "
                "Set THREADWEAVE_GRAPH_TENANT_ID, _CLIENT_ID, _CLIENT_SECRET."
            )
            return

        logger.info(
            "Sync daemon starting: interval=%ds, threadweave=%s",
            self.sync_interval, self.connector.threadweave_url,
        )

        self._running = True

        # Graceful shutdown on signals
        def _shutdown(signum, frame):
            logger.info("Received signal %s — shutting down.", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        # Startup: full sync
        try:
            stats = self.full_sync()
            logger.info("Initial full sync: %s", stats.to_dict())
        except Exception:
            logger.exception("Initial full sync failed — continuing.")

        # Loop: incremental syncs
        while self._running:
            time.sleep(self.sync_interval)

            if not self._running:
                break

            try:
                stats = self.incremental_sync()
                if stats.total_entries > 0 or stats.failed > 0:
                    logger.info("Incremental sync: %s", stats.to_dict())
            except Exception:
                logger.exception("Incremental sync failed — retrying next cycle.")

        logger.info("Sync daemon stopped.")

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._running = False

    # ── Health / Status ────────────────────────────────────────────

    def status(self) -> dict:
        """Get current sync status."""
        conn_status = self.connector.get_connection_status()
        return {
            "state": self.state.to_dict(),
            "connection": conn_status,
            "last_sync": (
                self._last_stats.to_dict() if self._last_stats else None
            ),
            "running": self._running,
            "graph_configured": self.connector.is_configured,
        }
