# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for entry versioning (edits link to the original)."""

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.store import EntryStore

client = TestClient(app)


def _doc_entry(eid, created, content="decision", version_of=None):
    return {
        "id": eid, "content": content, "wing": "mark_8_project_team",
        "room": "documents", "scope": "team", "source_type": "sharepoint",
        "author_id": "a@x.com", "title": "spec.txt",
        "created_at": created, "entities": [], "content_type": "decision",
        "has_pii": False, "tenant_id": "default",
        "source_metadata": {"source_file": "spec.txt"},
        "sensitivity": "internal", "client_id": None,
        "allowed_people": [], "version_of": version_of,
    }


def test_find_by_source_key(tmp_path):
    s = EntryStore(url=f"sqlite:///{tmp_path}/e.sqlite3")
    s.save(_doc_entry("e1", "2026-08-07T10:00:00+00:00"))
    s.save(_doc_entry("e2", "2026-08-07T11:00:00+00:00"))
    s.save(_doc_entry("e3", "2026-08-07T12:00:00+00:00"))
    s.save({**_doc_entry("other", "2026-08-07T12:00:00+00:00"),
            "source_metadata": {"source_file": "other.txt"}})

    matches = s.find_by_source_key("spec.txt")
    assert [m["id"] for m in matches] == ["e1", "e2", "e3"]
    assert s.find_by_source_key("") == []
    assert s.find_by_source_key("missing") == []


def test_version_of_roundtrip(tmp_path):
    path = str(tmp_path / "e.sqlite3")
    s1 = EntryStore(url=f"sqlite:///{path}")
    s1.save(_doc_entry("e1", "2026-08-07T10:00:00+00:00"))
    s1.save(_doc_entry("e2", "2026-08-07T11:00:00+00:00",
                       version_of="e1"))
    s2 = EntryStore(url=f"sqlite:///{path}")
    assert s2.get("e2")["version_of"] == "e1"
    assert s2.get("e1")["version_of"] is None


def test_migration_adds_version_of_to_existing_db(tmp_path):
    """An existing DB without version_of gets migrated on open."""
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE entries (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, wing TEXT DEFAULT '',
            room TEXT DEFAULT 'general', scope TEXT DEFAULT 'team',
            source_type TEXT DEFAULT 'manual', author_id TEXT DEFAULT 'unknown',
            title TEXT DEFAULT '', created_at TEXT, entities TEXT DEFAULT '[]',
            content_type TEXT DEFAULT 'answer', has_pii INTEGER DEFAULT 0,
            tenant_id TEXT DEFAULT 'default', source_metadata TEXT DEFAULT '{}',
            sensitivity TEXT DEFAULT 'internal', client_id TEXT,
            allowed_people TEXT DEFAULT '[]'
        )
    """)
    conn.execute(
        "INSERT INTO entries (id, content) VALUES ('legacy', 'old content')"
    )
    conn.commit()
    conn.close()

    s = EntryStore(url=f"sqlite:///{path}")
    entry = s.get("legacy")
    assert entry["id"] == "legacy"
    assert "version_of" in entry
    assert entry["version_of"] is None
    # New entries can carry version_of
    s.save(_doc_entry("e2", "2026-08-07T11:00:00+00:00", version_of="legacy"))
    assert s.get("e2")["version_of"] == "legacy"


# ---- API: version chaining on ingest ----

def test_ingest_chains_versions_for_same_source_file():
    # first capture of spec.txt
    r1 = client.post("/api/v1/ingest", json={
        "content": "We decided to use Redis for caching. The root cause "
                   "was the slow database reads, so we decided to add a "
                   "cache layer. This fixed the latency problem completely.",
        "source": "sharepoint", "tenant_id": "default",
        "metadata": {"source_file": "spec.txt", "title": "spec.txt"},
    })
    assert r1.json()["should_save"] is True
    id1 = r1.json()["id"]

    # edited re-capture of the same file
    r2 = client.post("/api/v1/ingest", json={
        "content": "We decided to use Redis with a TTL policy. The root "
                   "cause was stale cache entries, so we decided to add "
                   "expiry. This fixed the freshness problem completely.",
        "source": "sharepoint", "tenant_id": "default",
        "metadata": {"source_file": "spec.txt", "title": "spec.txt"},
    })
    assert r2.json()["should_save"] is True
    id2 = r2.json()["id"]

    # the new version points at the original
    r = client.get(f"/api/v1/entries/{id2}")
    assert r.json()["version_of"] == id1

    # the original has no parent
    r = client.get(f"/api/v1/entries/{id1}")
    assert r.json()["version_of"] == ""

    # versions endpoint returns both, oldest first
    r = client.get(f"/api/v1/entries/{id2}/versions")
    chain = r.json()["versions"]
    assert [v["id"] for v in chain] == [id1, id2]
    assert chain[0]["version_of"] is None or chain[0]["version_of"] == ""


def test_ingest_no_chain_without_source_file():
    r = client.post("/api/v1/ingest", json={
        "content": "We decided to use PostgreSQL for the auth service. "
                   "The root cause was the locking issues, so we decided "
                   "to switch databases. This fixed the problem completely.",
        "source": "teams", "tenant_id": "default",
        "metadata": {"author_id": "a@x.com"},
    })
    eid = r.json()["id"]
    assert r.json()["should_save"] is True
    r = client.get(f"/api/v1/entries/{eid}")
    assert r.json()["version_of"] == ""


def test_versions_endpoint_404():
    r = client.get("/api/v1/entries/nonexistent/versions")
    assert r.status_code == 404
