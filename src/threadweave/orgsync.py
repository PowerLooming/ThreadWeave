# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Org-sync daemon — who is in which team, kept current.

Polls the tenant's teams and their members via Graph, then posts each
team's membership snapshot to the API's /api/v1/org/sync endpoint for
temporal reconciliation (edges closed when people leave). Same
pull-only pattern as the other watchers; no webhooks.

Permissions on the Graph app registration:
- Team.ReadBasic.All  — enumerate teams
- TeamMember.Read.All — read each team's members

The org model stays in the API process; the daemon only feeds it.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 3600              # seconds between syncs (org
                                     # structure changes slowly)
DEFAULT_API_URL = "http://localhost:8000"

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


class OrgSyncDaemon:
    """Fetch teams + members from Graph and reconcile via the API."""

    def __init__(
        self,
        graph=None,
        api_base_url: str = "",
        sync: object | None = None,
        interval: int = DEFAULT_INTERVAL,
    ):
        from threadweave.connectors.sharepoint.watcher import GraphClient

        self.graph = graph or GraphClient()
        self.api_base_url = (
            api_base_url or os.environ.get(
                "THREADWEAVE_API_URL", DEFAULT_API_URL
            )
        )
        self.sync = sync  # test injection point: async fn(team) -> dict
        self.interval = interval
        self.stats = {
            "teams": 0, "members": 0, "synced": 0, "errors": 0,
            "added": 0, "closed": 0,
        }

    # ---- Graph reads ----

    async def list_teams(self) -> list[dict]:
        data = await self.graph._request_url(
            "GET",
            f"{GRAPH_API_BASE}/teams?$select=id,displayName",
        )
        return data.get("value", [])

    async def list_members(self, team_id: str) -> list[dict]:
        data = await self.graph._request_url(
            "GET",
            f"{GRAPH_API_BASE}/teams/{team_id}/members"
            "?$select=userId,displayName,email",
        )
        return data.get("value", [])

    # ---- Sync ----

    async def sync_team(self, team: dict) -> dict | None:
        team_id = team.get("id", "")
        if not team_id:
            return None
        members = await self.list_members(team_id)
        payload = {
            "team_id": team_id,
            "team_name": team.get("displayName", "") or team_id,
            "members": [
                {
                    "id": m.get("userId") or m.get("id") or "",
                    "name": m.get("displayName", ""),
                    "email": m.get("email", ""),
                }
                for m in members
                if m.get("userId") or m.get("id")
            ],
        }
        if self.sync is not None:
            return await self.sync(payload)
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.api_base_url}/api/v1/org/sync", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def run_once(self) -> dict:
        """One full sync cycle. Returns per-cycle stats."""
        cycle = {"teams": 0, "members": 0, "synced": 0, "errors": 0,
                 "added": 0, "closed": 0}
        try:
            teams = await self.list_teams()
        except Exception as e:
            cycle["errors"] += 1
            logger.error("list_teams failed: %s", e)
            return cycle

        cycle["teams"] = len(teams)
        for team in teams:
            try:
                result = await self.sync_team(team)
                if result is None:
                    continue
                cycle["synced"] += 1
                cycle["members"] += result.get("members", 0)
                cycle["added"] += result.get("added", 0)
                cycle["closed"] += result.get("closed", 0)
            except Exception as e:
                cycle["errors"] += 1
                logger.error(
                    "org sync failed for team %s: %s",
                    team.get("id", "?"), e,
                )

        for key, value in cycle.items():
            self.stats[key] = self.stats.get(key, 0) + value
        logger.info(
            "org sync: teams=%d members=%d added=%d closed=%d errors=%d",
            cycle["teams"], cycle["members"],
            cycle["added"], cycle["closed"], cycle["errors"],
        )
        return cycle

    async def run(self) -> None:
        """Continuous loop: sync, then sleep for the interval."""
        logger.info(
            "org-sync daemon: interval=%ds api=%s",
            self.interval, self.api_base_url,
        )
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("org sync cycle failed: %s", e)
            await asyncio.sleep(self.interval)
