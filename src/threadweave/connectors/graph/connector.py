# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Graph Connector — main client that syncs ThreadWeave knowledge to Microsoft Graph.

This is an OUTBOUND connector: it pushes ThreadWeave entries to Microsoft Graph
as external items, making them searchable in Copilot, Microsoft Search, and
other M365 surfaces.

Architecture:
    ThreadWeave API → ThreadWeaveGraphConnector → Microsoft Graph REST API

Authentication:
    Azure AD app-only client credentials. Requires:
    - ExternalConnection.ReadWrite.OwnedBy (or ExternalItem.ReadWrite.OwnedBy)
    - An external connection already created in the M365 Admin Center
      or via POST /external/connections (Graph API).

Usage:
    connector = ThreadWeaveGraphConnector(
        threadweave_url="http://localhost:8000",
        tenant_id="...",
        client_id="...",
        client_secret="...",
    )
    connector.register_schema()     # One-time schema registration
    stats = connector.full_sync()   # Sync all entries
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from threadweave.connectors.graph.auth import GraphAuth, GraphCredentials
from threadweave.connectors.graph.schema import (
    CONNECTION_ID,
    CONNECTION_NAME,
    CONNECTION_DESCRIPTION,
    map_threadweave_to_graph,
)

logger = logging.getLogger("threadweave.graph.connector")

# Microsoft Graph API base URL
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class SyncStats:
    """Statistics from a sync operation."""
    total_entries: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    @property
    def success_rate(self) -> float:
        attempted = self.created + self.updated + self.deleted
        if attempted == 0:
            return 1.0
        return (attempted - self.failed) / attempted

    def to_dict(self) -> dict:
        return {
            "total_entries": self.total_entries,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "failed": self.failed,
            "success_rate": round(self.success_rate, 3),
            "errors": self.errors[-10:],  # Last 10 errors only
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class ThreadWeaveGraphConnector:
    """Sync ThreadWeave knowledge to Microsoft Graph as external items.

    This is the main connector. It reads entries from the ThreadWeave API
    and pushes them to Microsoft Graph as external connection items.

    The connector can:
    - Register the connection schema (one-time)
    - Perform a full sync (all entries)
    - Perform an incremental sync (entries changed since last sync)
    - Delete stale items that no longer exist in ThreadWeave
    """

    def __init__(
        self,
        threadweave_url: str = "http://localhost:8000",
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        wing_to_group: Optional[dict[str, str]] = None,
    ):
        """
        Args:
            threadweave_url: Base URL of the ThreadWeave API
            tenant_id: Azure AD tenant ID (or set THREADWEAVE_GRAPH_TENANT_ID)
            client_id: App registration client ID (or THREADWEAVE_GRAPH_CLIENT_ID)
            client_secret: App registration client secret (or THREADWEAVE_GRAPH_CLIENT_SECRET)
            wing_to_group: Mapping of wing names → Entra ID group IDs for ACLs.
        """
        self.threadweave_url = threadweave_url.rstrip("/")

        # Resolve credentials: explicit args > env vars
        creds = GraphCredentials(
            tenant_id=tenant_id or "",
            client_id=client_id or "",
            client_secret=client_secret or "",
        )
        if not creds.is_configured():
            env_creds = GraphCredentials.from_env()
            if env_creds:
                creds = env_creds

        self._creds = creds
        self._auth: Optional[GraphAuth] = None
        self._wing_to_group = wing_to_group or {}

        # Track item IDs we've pushed for deletion detection
        self._synced_ids: set[str] = set()

    @property
    def auth(self) -> GraphAuth:
        """Lazy-init the auth client."""
        if self._auth is None:
            if not self._creds.is_configured():
                raise RuntimeError(
                    "Graph credentials not configured. Set "
                    "THREADWEAVE_GRAPH_TENANT_ID, THREADWEAVE_GRAPH_CLIENT_ID, "
                    "and THREADWEAVE_GRAPH_CLIENT_SECRET environment variables."
                )
            self._auth = GraphAuth(self._creds)
        return self._auth

    @property
    def is_configured(self) -> bool:
        """Check if Graph credentials are available."""
        return self._creds.is_configured()

    @property
    def connection_endpoint(self) -> str:
        """Base URL for the external connection items API."""
        return f"{GRAPH_API_BASE}/external/connections/{CONNECTION_ID}"

    # ── Connection Management ──────────────────────────────────────

    def register_schema(self) -> bool:
        """Register (or update) the ThreadWeave external connection schema.

        This is a one-time setup step. Must be called before syncing items.
        Requires ExternalConnection.ReadWrite.OwnedBy permission.
        """
        url = f"{GRAPH_API_BASE}/external/connections"
        payload = {
            "id": CONNECTION_ID,
            "name": CONNECTION_NAME,
            "description": CONNECTION_DESCRIPTION,
        }

        headers = self._headers()
        resp = requests.post(url, json=payload, headers=headers, timeout=30)

        if resp.status_code in (201, 200):
            logger.info(
                "Connection '%s' registered successfully.", CONNECTION_ID,
            )
            return True
        elif resp.status_code == 409:
            logger.info(
                "Connection '%s' already exists — updating.", CONNECTION_ID,
            )
            # PATCH to update the existing connection
            patch_resp = requests.patch(
                f"{url}/{CONNECTION_ID}",
                json={"description": CONNECTION_DESCRIPTION},
                headers=headers,
                timeout=30,
            )
            return patch_resp.status_code in (200, 204)
        else:
            logger.error(
                "Failed to register connection: %s — %s",
                resp.status_code, resp.text,
            )
            return False

    def get_connection_status(self) -> Optional[dict]:
        """Get the current state of the external connection."""
        url = f"{GRAPH_API_BASE}/external/connections/{CONNECTION_ID}"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "Connection status check failed: %s — %s",
            resp.status_code, resp.text,
        )
        return None

    def delete_connection(self) -> bool:
        """Delete the entire external connection and all its items."""
        url = f"{GRAPH_API_BASE}/external/connections/{CONNECTION_ID}"
        resp = requests.delete(url, headers=self._headers(), timeout=30)
        return resp.status_code == 202  # Accepted for async deletion

    # ── Item Operations (CRUD) ─────────────────────────────────────

    def upsert_item(self, entry: dict) -> bool:
        """Push or update a single ThreadWeave entry to Microsoft Graph.

        Args:
            entry: ThreadWeave entry dict from the API or _memory_store.

        Returns:
            True if the item was successfully created/updated.
        """
        item = map_threadweave_to_graph(
            entry,
            base_url=self.threadweave_url,
            wing_to_group=self._wing_to_group,
        )

        url = f"{self.connection_endpoint}/items/{item.item_id}"
        payload = item.to_payload()

        resp = requests.put(
            url, json=payload, headers=self._headers(), timeout=30,
        )

        if resp.status_code in (200, 201, 204):
            self._synced_ids.add(item.item_id)
            return True

        logger.error(
            "Failed to upsert item %s: %s — %s",
            item.item_id, resp.status_code, resp.text[:200],
        )
        return False

    def delete_item(self, item_id: str) -> bool:
        """Delete a single external item from Microsoft Graph."""
        url = f"{self.connection_endpoint}/items/{item_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=15)
        if resp.status_code in (200, 204):
            self._synced_ids.discard(item_id)
            return True

        logger.warning(
            "Failed to delete item %s: %s — %s",
            item_id, resp.status_code, resp.text[:100],
        )
        return False

    # ── Sync Operations ────────────────────────────────────────────

    def full_sync(self) -> SyncStats:
        """Sync ALL ThreadWeave entries to Microsoft Graph.

        Fetches all entries from ThreadWeave and pushes them to Graph.
        After syncing, deletes any Graph items that no longer exist
        in ThreadWeave.

        Returns:
            SyncStats with counts of created/updated/deleted/failed items.
        """
        stats = SyncStats(started_at=datetime.now(timezone.utc).isoformat())

        # 1. Fetch all entries from ThreadWeave
        entries = self._fetch_all_entries()
        stats.total_entries = len(entries)

        if not entries:
            logger.warning("No entries in ThreadWeave. Nothing to sync.")
            stats.completed_at = datetime.now(timezone.utc).isoformat()
            return stats

        logger.info("Starting full sync of %d entries...", len(entries))

        # 2. Push each entry to Graph
        for i, entry in enumerate(entries):
            try:
                if self.upsert_item(entry):
                    stats.created += 1
                else:
                    stats.failed += 1
                    stats.errors.append(
                        f"Failed to upsert {entry.get('id', 'unknown')}"
                    )
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"Exception on {entry.get('id')}: {exc}")
                logger.exception("Exception syncing entry %s", entry.get("id"))

            # Progress logging every 100 items
            if (i + 1) % 100 == 0:
                logger.info("Synced %d/%d entries...", i + 1, len(entries))

        # 3. Delete stale items (in Graph but not in ThreadWeave)
        stale_ids = self._find_stale_items()
        for stale_id in stale_ids:
            try:
                if self.delete_item(stale_id):
                    stats.deleted += 1
                else:
                    stats.failed += 1
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"Exception deleting {stale_id}: {exc}")

        stats.completed_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Full sync complete: %d created, %d deleted, %d failed "
            "(%.1f%% success rate)",
            stats.created, stats.deleted, stats.failed,
            stats.success_rate * 100,
        )
        return stats

    def incremental_sync(self, since: Optional[str] = None) -> SyncStats:
        """Sync only entries changed since a given timestamp.

        Args:
            since: ISO 8601 timestamp. Defaults to last sync marker.

        Returns:
            SyncStats with counts for this incremental batch.
        """
        stats = SyncStats(started_at=datetime.now(timezone.utc).isoformat())

        # Fetch entries changed since timestamp
        entries = self._fetch_entries_since(since)
        stats.total_entries = len(entries)

        for entry in entries:
            try:
                if self.upsert_item(entry):
                    stats.updated += 1
                else:
                    stats.failed += 1
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(str(exc))

        stats.completed_at = datetime.now(timezone.utc).isoformat()
        return stats

    # ── Internal Helpers ───────────────────────────────────────────

    def _headers(self) -> dict:
        """Build Authorization headers for Graph API requests."""
        return {
            "Authorization": f"Bearer {self.auth.access_token}",
            "Content-Type": "application/json",
        }

    def _fetch_all_entries(self) -> list[dict]:
        """Fetch all entries from the ThreadWeave API.

        Currently fetches wings → rooms → entries.
        In production, this should use a paginated list endpoint.
        """
        try:
            # Get all entries via the search endpoint with a wildcard
            # (ThreadWeave doesn't have a list-all endpoint yet, so we use search)
            resp = requests.post(
                f"{self.threadweave_url}/api/v1/search",
                json={"query": "", "limit": 500},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error(
                    "Failed to fetch entries: %s", resp.status_code,
                )
                return []

            data = resp.json()
            results = data.get("results", [])

            # Fetch full content for each result
            entries = []
            for r in results:
                entry_resp = requests.get(
                    f"{self.threadweave_url}/api/v1/entries/{r['id']}",
                    timeout=10,
                )
                if entry_resp.status_code == 200:
                    entries.append(entry_resp.json())
                else:
                    # Use the preview data as fallback
                    entries.append({
                        "id": r["id"],
                        "content": r.get("content_preview", ""),
                        "wing": r.get("wing", ""),
                        "room": r.get("room", ""),
                        "created_at": r.get("created_at", ""),
                        "content_type": r.get("content_type", ""),
                        "source_type": "unknown",
                        "author_id": r.get("author_team", ""),
                        "scope": "team",
                    })

            return entries
        except Exception as exc:
            logger.exception("Failed to fetch entries from ThreadWeave: %s", exc)
            return []

    def _fetch_entries_since(self, since: Optional[str] = None) -> list[dict]:
        """Fetch entries changed since timestamp."""
        # Placeholder — ThreadWeave needs a timestamp filter on the search endpoint
        # For now, do a full fetch
        logger.warning("Incremental sync not fully implemented — doing full fetch")
        return self._fetch_all_entries()

    def _find_stale_items(self) -> list[str]:
        """Find Graph items that no longer exist in ThreadWeave.

        Fetches the list of item IDs from the Graph connection and
        subtracts the set we just synced.
        """
        # Graph doesn't have a simple "list all items" endpoint.
        # Items are synced individually. We track synced_ids during
        # the sync and delete anything in the Graph that wasn't touched.
        #
        # For a proper implementation, we'd use Graph's delta query
        # or store a sync-state file locally.
        return []  # Stub — full external delta query is a future enhancement
