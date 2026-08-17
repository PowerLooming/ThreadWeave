# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Org tracker: reconciliation model, API sync endpoint, daemon."""

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.org_model import OrgModel
from threadweave.orgsync import OrgSyncDaemon


# ---- OrgModel reconciliation ----

def test_sync_team_membership_adds_and_closes():
    model = OrgModel()
    result = model.sync_team_membership(
        "team-1", ["alice", "bob"], "2026-08-01"
    )
    assert result == {"added": 2, "closed": 0, "members": 2}
    assert model.get_team("alice") == "team-1"
    assert model.get_team("bob") == "team-1"

    # Bob leaves: the edge closes, Alice stays.
    result = model.sync_team_membership(
        "team-1", ["alice"], "2026-08-17"
    )
    assert result == {"added": 0, "closed": 1, "members": 1}
    assert model.get_team("alice") == "team-1"
    assert model.get_team("bob") is None

    # Temporal view: Bob was a member before the close date.
    assert model.get_team("bob", as_of="2026-08-10") == "team-1"


def test_sync_team_membership_idempotent():
    model = OrgModel()
    model.sync_team_membership("team-1", ["alice"], "2026-08-01")
    result = model.sync_team_membership("team-1", ["alice"], "2026-08-02")
    assert result == {"added": 0, "closed": 0, "members": 1}
    assert len(model.relationships) == 1


# ---- API endpoint ----

def test_org_sync_endpoint_and_resolution():
    client = TestClient(app)
    r = client.post("/api/v1/org/sync", json={
        "team_id": "team-9",
        "team_name": "Billing",
        "members": [
            {"id": "user-1", "name": "Alice", "email": "alice@x.com"},
            {"id": "user-2", "name": "Bob", "email": "bob@x.com"},
        ],
        "valid_from": "2026-08-17",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "synced"
    assert body["added"] == 2

    team = client.get("/api/v1/org/people/user-1/team").json()
    assert team["team"] == "team-9"

    graph = client.get("/api/v1/org/graph").json()
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "team-9" in node_ids
    assert "user-1" in node_ids


# ---- Daemon ----

class FakeGraph:
    def __init__(self, teams, members_by_team):
        self.teams = teams
        self.members_by_team = members_by_team
        self.calls = []

    async def _request_url(self, method, url):
        self.calls.append(url)
        if "/members" in url:
            team_id = url.split("/teams/")[1].split("/members")[0]
            return {"value": self.members_by_team.get(team_id, [])}
        return {"value": self.teams}


class FakeSync:
    def __init__(self):
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(payload)
        return {"added": len(payload["members"]), "closed": 0,
                "members": len(payload["members"])}


@pytest.mark.asyncio
async def test_daemon_run_once_syncs_all_teams():
    graph = FakeGraph(
        teams=[
            {"id": "t1", "displayName": "Retail"},
            {"id": "t2", "displayName": "Sales"},
        ],
        members_by_team={
            "t1": [
                {"userId": "u1", "displayName": "Alice",
                 "email": "alice@x.com"},
            ],
            "t2": [
                {"userId": "u2", "displayName": "Bob",
                 "email": "bob@x.com"},
                {"userId": "u3", "displayName": "Carol",
                 "email": "carol@x.com"},
            ],
        },
    )
    sink = FakeSync()
    daemon = OrgSyncDaemon(graph=graph, sync=sink)

    cycle = await daemon.run_once()

    assert cycle["teams"] == 2
    assert cycle["members"] == 3
    assert cycle["synced"] == 2
    assert cycle["errors"] == 0
    assert len(sink.payloads) == 2
    assert sink.payloads[0]["team_id"] == "t1"
    assert sink.payloads[0]["team_name"] == "Retail"
    assert sink.payloads[0]["members"][0]["id"] == "u1"


@pytest.mark.asyncio
async def test_daemon_isolates_team_failures():
    class FailingSync:
        def __init__(self):
            self.n = 0

        async def __call__(self, payload):
            self.n += 1
            if payload["team_id"] == "t1":
                raise RuntimeError("boom")
            return {"added": 0, "closed": 0, "members": 0}

    graph = FakeGraph(
        teams=[{"id": "t1", "displayName": "A"},
               {"id": "t2", "displayName": "B"}],
        members_by_team={"t1": [], "t2": []},
    )
    daemon = OrgSyncDaemon(graph=graph, sync=FailingSync())

    cycle = await daemon.run_once()
    assert cycle["synced"] == 1
    assert cycle["errors"] == 1


# ---- CLI registration ----

def test_org_sync_registered_as_daemon():
    from threadweave.daemons import DAEMONS

    assert "org-sync" in DAEMONS
    assert DAEMONS["org-sync"]["argv"][-2:] == ["org", "sync"]


def test_org_sync_cli_parser():
    from threadweave.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["org", "sync", "--interval", "60"])
    assert args.org_command == "sync"
    assert args.interval == 60
