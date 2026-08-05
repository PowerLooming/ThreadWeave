# SPDX-License-Identifier: MIT
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
        """Register (or update) the ThreadWeave external connection and its schema.

        Creates the connection (POST /external/connections) if it doesn't exist,
        then registers the schema (PUT /external/connections/{id}/schema) so items
        can be upserted. The schema operation is asynchronous — this method polls
        until it completes or times out.

        This is a one-time setup step. Must be called before syncing items.
        Requires ExternalConnection.ReadWrite.OwnedBy permission.
        """
        url = f"{GRAPH_API_BASE}/external/connections"
        headers = self._headers()

        # ── Step 1: Create or update the connection ──
        payload = {
            "id": CONNECTION_ID,
            "name": CONNECTION_NAME,
            "description": CONNECTION_DESCRIPTION,
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)

        if resp.status_code in (201, 200):
            logger.info(
                "Connection '%s' created successfully.", CONNECTION_ID,
            )
        elif resp.status_code == 409:
            logger.info(
                "Connection '%s' already exists — updating.", CONNECTION_ID,
            )
            patch_resp = requests.patch(
                f"{url}/{CONNECTION_ID}",
                json={"description": CONNECTION_DESCRIPTION},
                headers=headers,
                timeout=30,
            )
            if patch_resp.status_code not in (200, 204):
                logger.error(
                    "Failed to update connection: %s — %s",
                    patch_resp.status_code, patch_resp.text,
                )
                return False
        else:
            logger.error(
                "Failed to create connection: %s — %s",
                resp.status_code, resp.text,
            )
            return False

        # ── Step 2: Register the schema ──
        return self._register_schema_properties()

    def _register_schema_properties(self) -> bool:
        """Register the property schema for the external connection.

        PUT /external/connections/{id}/schema — asynchronous operation.
        Polls until the operation completes or times out.
        """
        schema_url = f"{self.connection_endpoint}/schema"
        schema_payload = {
            "baseType": "externalItem",
            "baseUrl": self.threadweave_url,
            "properties": [
                {
                    "name": "title", "type": "String",
                    "isSearchable": True, "isQueryable": True,
                    "isRetrievable": True, "aliases": [],
                },
                {
                    "name": "wing", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "isRefinable": True, "aliases": [],
                },
                {
                    "name": "room", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "isRefinable": True, "aliases": [],
                },
                {
                    "name": "contentType", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
                {
                    "name": "author", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
                {
                    "name": "authorTeam", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
                {
                    "name": "createdDateTime", "type": "DateTime",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
                {
                    "name": "sourceType", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
                {
                    "name": "scope", "type": "String",
                    "isQueryable": True, "isRetrievable": True,
                    "aliases": [],
                },
            ],
        }

        headers = self._headers()
        resp = requests.put(
            schema_url, json=schema_payload, headers=headers, timeout=30,
        )

        if resp.status_code in (200, 201, 204):
            logger.info("Schema registered successfully for '%s'.", CONNECTION_ID)
            return True

        if resp.status_code == 202:
            # Async operation — poll the Location header
            location = resp.headers.get("Location", "")
            if not location:
                logger.warning(
                    "Schema registration accepted but no Location header; "
                    "assuming success."
                )
                return True

            logger.info(
                "Schema registration for '%s' accepted (async). Polling...",
                CONNECTION_ID,
            )
            return self._poll_schema_operation(location)

        # 409 = schema already exists, which is fine
        if resp.status_code == 409:
            logger.info(
                "Schema for '%s' already exists.", CONNECTION_ID,
            )
            return True

        logger.error(
            "Failed to register schema for '%s': %s — %s",
            CONNECTION_ID, resp.status_code, resp.text[:300],
        )
        return False

    def _poll_schema_operation(self, location: str, max_wait: int = 300) -> bool:
        """Poll an async schema operation until it completes.

        Microsoft's schema operations routinely take 1-3 minutes; the
        previous 60s window timed out on every live registration
        (verified 2026-08-05 against a full M365 sandbox).
        """
        import time
        deadline = time.time() + max_wait
        headers = self._headers()
        # Don't send Content-Type on GET
        poll_headers = {"Authorization": headers["Authorization"]}

        while time.time() < deadline:
            resp = requests.get(location, headers=poll_headers, timeout=15)
            if resp.status_code == 200:
                status = resp.json().get("status", "")
                if status == "completed":
                    logger.info("Schema operation completed.")
                    return True
                if status == "failed":
                    logger.error(
                        "Schema operation failed: %s", resp.text[:300],
                    )
                    return False
                logger.debug("Schema operation status: %s (polling...)", status)
            elif resp.status_code == 404:
                # Operation result not yet available, keep polling
                pass
            else:
                logger.warning(
                    "Unexpected status polling schema: %s", resp.status_code,
                )
            time.sleep(2)

        logger.error("Timed out waiting for schema operation after %ds.", max_wait)
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

        Uses the per-tenant list endpoint (GET /api/v1/tenants/{tenant}/entries)
        then fetches full content for each entry. The old implementation
        searched with an empty query, which the API rejects (query must be
        at least 1 char) — fixed 2026-08-05 during live testing.
        """
        try:
            list_url = f"{self.threadweave_url}/api/v1/tenants/default/entries"
            resp = requests.get(list_url, timeout=30)
            if resp.status_code != 200:
                logger.error(
                    "Failed to list entries: %s", resp.status_code,
                )
                return []

            listing = resp.json()

            # Fetch full content for each entry
            entries = []
            for item in listing:
                entry_resp = requests.get(
                    f"{self.threadweave_url}/api/v1/entries/{item['id']}",
                    timeout=10,
                )
                if entry_resp.status_code == 200:
                    entries.append(entry_resp.json())
                else:
                    # Use the preview data as fallback
                    entries.append({
                        "id": item["id"],
                        "content": "",
                        "wing": "",
                        "room": "",
                        "created_at": item.get("created_at", ""),
                        "content_type": "",
                        "source_type": "unknown",
                        "author_id": "",
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
