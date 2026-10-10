# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""P3: the owner's interface for the peer link.

Issuance, revocation, the read report, and the bridge edge in the palace graph.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from threadweave import auth as auth_mod

ADMIN_KEY = "sk-admin-p3"
TENANT_KEY = "sk-tenant-p3"
PEER_KEY = "sk-peer-nordvik-p3"
PEER_ID = "nordvik"


def _keys_doc() -> dict:
    return {"keys": [
        {"key": ADMIN_KEY, "tenant_id": "*", "role": "admin", "label": "owner"},
        {"key": TENANT_KEY, "tenant_id": "default", "role": "readwrite",
         "wing": "engineering", "person_id": "alice"},
        {"key": PEER_KEY, "tenant_id": "default", "role": "readonly",
         "wing": "engineering", "label": f"peer:{PEER_ID}", "peer_id": PEER_ID,
         "peer_contact": "security@example.invalid", "issued_by": "harald",
         "issued_at": "2026-10-10T00:00:00+00:00",
         "expires_at": "2099-01-01T00:00:00+00:00", "max_results": 2},
    ]}


@pytest.fixture
def owner_world(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".threadweave").mkdir(parents=True)
    (home / ".threadweave" / "keys.json").write_text(json.dumps(_keys_doc()))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("THREADWEAVE_REQUIRE_AUTH", "1")
    auth_mod.reset()
    auth_mod.reset_peer_rate_limits()
    yield home
    monkeypatch.delenv("THREADWEAVE_REQUIRE_AUTH", raising=False)
    auth_mod.configure(enabled=False)


@pytest.fixture
def api():
    from threadweave.api import app

    return TestClient(app)


def _save(client, *, wing: str, content: str, room: str = "architecture") -> str:
    resp = client.post("/api/v1/entries", json={
        "content": content, "wing": wing, "room": room, "source_type": "manual",
        "author_id": "alice", "scope": "team", "sensitivity": "internal",
    }, headers={"X-API-Key": TENANT_KEY})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# ── The owner's listing ──────────────────────────────────────

def test_a_peer_key_cannot_read_the_owner_view(owner_world, api):
    resp = api.get("/api/v1/peers", headers={"X-API-Key": PEER_KEY})
    assert resp.status_code == 403
    assert "may only search" in resp.json()["error"]


def test_the_owner_sees_the_peer_its_bridge_and_the_allowlist(owner_world, api):
    resp = api.get("/api/v1/peers", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    peers = {p["peer_id"]: p for p in body["peers"]}
    assert PEER_ID in peers
    assert peers[PEER_ID]["wing"] == "engineering"
    assert peers[PEER_ID]["max_results"] == 2
    # The key value is never part of this view.
    assert "key" not in json.dumps(body["peers"][0] if body["peers"] else {})
    assert body["bridges"] == [{
        "id": f"peer:{PEER_ID}", "peer_id": PEER_ID, "wing": "engineering",
        "label": "security@example.invalid", "expires_at": "2099-01-01T00:00:00+00:00",
        "max_results": 2, "bridge": True,
    }]
    assert body["allowlist"] == ["POST /api/v1/search", "GET /api/v1/entries/{entry_id}"]


# ── Issuance ─────────────────────────────────────────────────

def test_issuing_requires_an_admin_key(owner_world, api):
    resp = api.post("/api/v1/peers", json={"peer_id": "acme", "wing": "engineering"},
                    headers={"X-API-Key": TENANT_KEY})
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]


def test_an_admin_issues_a_key_that_works_immediately(owner_world, api):
    issued = api.post("/api/v1/peers", json={
        "peer_id": "acme", "wing": "engineering", "contact": "ops@example.invalid",
        "days": 30, "max_results": 5,
    }, headers={"X-API-Key": ADMIN_KEY})
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["key"].startswith("sk-peer-")
    assert body["wing"] == "engineering"
    assert body["max_results"] == 5

    # No restart, no reload call: the running server picks the new key up from
    # the file it re-reads when it changes.
    _save(api, wing="engineering", content="postgres was chosen for the ledger.")
    search = api.post("/api/v1/search", json={"query": "postgres", "limit": 5},
                      headers={"X-API-Key": body["key"]})
    assert search.status_code == 200, search.text

    # And the owner can see it, with the person who issued it recorded.
    listed = api.get("/api/v1/peers", headers={"X-API-Key": ADMIN_KEY}).json()
    acme = {p["peer_id"]: p for p in listed["peers"]}["acme"]
    assert acme["issued_by"] == "owner"
    assert acme["contact"] == "ops@example.invalid"


def test_a_second_active_key_for_one_peer_is_refused(owner_world, api):
    payload = {"peer_id": "acme", "wing": "engineering"}
    assert api.post("/api/v1/peers", json=payload, headers={"X-API-Key": ADMIN_KEY}).status_code == 200
    again = api.post("/api/v1/peers", json=payload, headers={"X-API-Key": ADMIN_KEY})
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


def test_issuing_without_a_wing_is_rejected(owner_world, api):
    resp = api.post("/api/v1/peers", json={"peer_id": "acme"}, headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 422


# ── Revocation ───────────────────────────────────────────────

def test_an_admin_revokes_and_access_stops_on_the_next_request(owner_world, api):
    _save(api, wing="engineering", content="postgres migrations run on deploy.")
    assert api.post("/api/v1/search", json={"query": "postgres", "limit": 5},
                    headers={"X-API-Key": PEER_KEY}).status_code == 200

    revoked = api.post(f"/api/v1/peers/{PEER_ID}/revoke", headers={"X-API-Key": ADMIN_KEY})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] == 1

    after = api.post("/api/v1/search", json={"query": "postgres", "limit": 5},
                     headers={"X-API-Key": PEER_KEY})
    assert after.status_code == 403
    assert "invalid api key" in after.json()["error"].lower()

    # A revoked peer leaves the bridges with it.
    graph = api.get("/api/v1/org/graph", headers={"X-API-Key": ADMIN_KEY}).json()
    assert not [e for e in graph["edges"] if e.get("type") == "bridge"]


def test_revoking_an_unknown_peer_is_a_404(owner_world, api):
    resp = api.post("/api/v1/peers/nobody/revoke", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 404


def test_revoking_requires_admin(owner_world, api):
    resp = api.post(f"/api/v1/peers/{PEER_ID}/revoke", headers={"X-API-Key": TENANT_KEY})
    assert resp.status_code == 403


# ── The report, and the bridge in the graph ──────────────────

def test_the_owner_report_counts_what_the_peer_read(owner_world, api):
    entry_id = _save(api, wing="engineering", content="postgres was chosen for the ledger service.")
    api.post("/api/v1/search", json={"query": "postgres", "limit": 5},
             headers={"X-API-Key": PEER_KEY})
    api.get(f"/api/v1/entries/{entry_id}", headers={"X-API-Key": PEER_KEY})

    listed = api.get("/api/v1/peers", headers={"X-API-Key": ADMIN_KEY}).json()
    peer = {p["peer_id"]: p for p in listed["peers"]}[PEER_ID]
    activity = peer["activity"]
    assert activity["reads"] >= 1
    assert activity["searches"] >= 1
    assert activity["wings_read"] == ["engineering"]
    assert entry_id in activity["entries_read"]
    assert activity["last_read"]


def test_the_graph_shows_the_bridge_into_the_wing(owner_world, api):
    graph = api.get("/api/v1/org/graph", headers={"X-API-Key": ADMIN_KEY}).json()
    bridges = [e for e in graph["edges"] if e.get("type") == "bridge"]
    assert bridges, graph["edges"]
    bridge = bridges[0]
    assert bridge["relation"] == "may_reach"
    assert bridge["peer_id"] == PEER_ID
    assert bridge["target"] == "engineering"
    peer_nodes = [n for n in graph["nodes"] if n.get("type") == "peer"]
    assert peer_nodes and peer_nodes[0]["id"] == f"peer:{PEER_ID}"

    # A subgraph for another wing does not drag this partner into it.
    other = api.get("/api/v1/org/graph?wing=sales", headers={"X-API-Key": ADMIN_KEY}).json()
    assert not [e for e in other["edges"] if e.get("type") == "bridge"]
