# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for knowledge entry nodes in the org graph."""

from fastapi.testclient import TestClient

from threadweave.api import app, _entry_graph_nodes
from threadweave.store import get_entry_store

client = TestClient(app)


def _seed_entries(entries: list[dict]) -> list[str]:
    """Save entries directly to the store; return ids."""
    ids = []
    for e in entries:
        entry = {
            "id": e["id"], "content": e["content"], "wing": e["wing"],
            "room": e.get("room", "documents"), "scope": "team",
            "source_type": e.get("source", "teams"),
            "author_id": e.get("author_id", "unknown"),
            "title": e.get("title", ""), "created_at": "2026-08-08T10:00:00+00:00",
            "entities": [], "content_type": "decision", "has_pii": False,
            "tenant_id": "default", "source_metadata": {},
            "sensitivity": "internal", "client_id": None,
            "allowed_people": [], "version_of": None,
        }
        get_entry_store().save(entry)
        ids.append(entry["id"])
    return ids


def test_entry_graph_nodes_edges():
    ids = _seed_entries([
        {"id": "e1", "content": "decided to use Redis",
         "wing": "engineering", "author_id": "alice",
         "title": "Cache decision"},
        {"id": "e2", "content": "decided to use Postgres",
         "wing": "engineering", "author_id": "bob",
         "title": "DB decision"},
        {"id": "e3", "content": "external sender content",
         "wing": "email", "author_id": "someone@outside.com",
         "title": "External mail"},
    ])

    nodes, edges = _entry_graph_nodes(
        author_ids={"alice": "Alice", "bob": "Bob"},
        known_node_ids={"engineering", "alice", "bob"},
    )
    try:
        # my entries are present as entry nodes (other tests may add more)
        entry_ids = {eid for eid, n in nodes.items() if n["type"] == "entry"}
        assert set(ids) <= entry_ids

        # e1: belongs_to engineering (known) + authored_by alice (known)
        rels_e1 = [(e["relation"], e["target"]) for e in edges
                   if e["source"] == "e1"]
        assert ("belongs_to", "engineering") in rels_e1
        assert ("authored_by", "alice") in rels_e1

        # e3: wing 'email' not a node -> no belongs_to; author unknown
        # -> no authored_by (no dangling edges)
        rels_e3 = [e for e in edges if e["source"] == "e3"]
        assert rels_e3 == []

        # node carries room/source for the dashboard
        assert nodes["e1"]["room"] == "documents"
        assert nodes["e1"]["source"] == "teams"
    finally:
        for eid in ids:
            get_entry_store().delete(eid)


def test_entry_graph_nodes_cap():
    ids = _seed_entries([
        {"id": f"cap{i}", "content": f"decision {i}",
         "wing": "engineering", "author_id": "alice",
         "title": f"Decision {i}"}
        for i in range(5)
    ])
    try:
        nodes, _ = _entry_graph_nodes(max_per_wing=3)
        entry_ids = [eid for eid, n in nodes.items() if n["type"] == "entry"]
        assert len(entry_ids) == 3
    finally:
        for eid in ids:
            get_entry_store().delete(eid)


def test_graph_endpoint_includes_entries():
    ids = _seed_entries([
        {"id": "ge1", "content": "decided to use Redis for sessions",
         "wing": "engineering", "author_id": "alice",
         "title": "Redis for sessions"},
    ])
    try:
        client.post("/api/v1/org/relationships", json={
            "source": "alice", "relation": "member_of",
            "target": "engineering", "valid_from": "2024-01-01",
        })
        client.post("/api/v1/org/relationships", json={
            "source": "engineering", "relation": "subteam_of",
            "target": "root", "valid_from": "2024-01-01",
        })

        resp = client.get("/api/v1/org/graph")
        assert resp.status_code == 200
        data = resp.json()
        entry_nodes = [n for n in data["nodes"] if n.get("type") == "entry"]
        # other tests may seed entries; ours must be present
        assert any(n["id"] == "ge1" for n in entry_nodes)
        ge1 = next(n for n in entry_nodes if n["id"] == "ge1")
        assert ge1["label"] == "Redis for sessions"

        # belongs_to engineering (wing is a node in the full graph)
        belongs = [e for e in data["edges"]
                   if e["source"] == "ge1" and e["relation"] == "belongs_to"]
        assert belongs and belongs[0]["target"] == "engineering"

        # authored_by alice (person node exists)
        authored = [e for e in data["edges"]
                    if e["source"] == "ge1" and e["relation"] == "authored_by"]
        assert authored and authored[0]["target"] == "alice"
    finally:
        for eid in ids:
            get_entry_store().delete(eid)
