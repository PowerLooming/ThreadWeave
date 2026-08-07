# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the durable entry store (SQLite persistence)."""

import pytest

from threadweave.store import EntryStore


@pytest.fixture
def store(tmp_path):
    return EntryStore(db_path=str(tmp_path / "entries.sqlite3"))


def _entry(eid="abc123", tenant="default", wing="engineering"):
    return {
        "id": eid,
        "content": "We decided to use PostgreSQL for the auth service",
        "wing": wing,
        "room": "decision",
        "scope": "team",
        "source_type": "email",
        "author_id": "alice@company.com",
        "title": "PostgreSQL decision",
        "created_at": "2026-08-07T10:00:00+00:00",
        "entities": [{"type": "technology", "value": "PostgreSQL"}],
        "content_type": "decision",
        "has_pii": False,
        "tenant_id": tenant,
        "source_metadata": {"email_sender": "alice@company.com"},
        "sensitivity": "internal",
        "client_id": None,
        "allowed_people": [],
    }


def test_save_and_load_roundtrip(store):
    store.save(_entry())
    entries = store.load_all()
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == "abc123"
    assert e["wing"] == "engineering"
    assert e["content_type"] == "decision"
    assert e["entities"][0]["value"] == "PostgreSQL"
    assert e["source_metadata"]["email_sender"] == "alice@company.com"
    assert e["has_pii"] is False


def test_survives_restart(tmp_path):
    """New store instance on the same DB file = the palace survived."""
    path = str(tmp_path / "entries.sqlite3")
    EntryStore(db_path=path).save(_entry("e1"))
    EntryStore(db_path=path).save(_entry("e2", tenant="lqdx", wing="Retail"))

    restarted = EntryStore(db_path=path)
    entries = restarted.load_all()
    assert len(entries) == 2
    by_id = {e["id"]: e for e in entries}
    assert by_id["e1"]["tenant_id"] == "default"
    assert by_id["e2"]["tenant_id"] == "lqdx"
    assert by_id["e2"]["wing"] == "Retail"


def test_delete_removes_persistently(tmp_path):
    path = str(tmp_path / "entries.sqlite3")
    s1 = EntryStore(db_path=path)
    s1.save(_entry("keep"))
    s1.save(_entry("gone"))
    s1.delete("gone")

    s2 = EntryStore(db_path=path)
    entries = s2.load_all()
    assert [e["id"] for e in entries] == ["keep"]


def test_get_single(store):
    store.save(_entry("only"))
    assert store.get("only")["title"] == "PostgreSQL decision"
    assert store.get("nope") is None


def test_count(store):
    assert store.count() == 0
    store.save(_entry("a"))
    store.save(_entry("b"))
    assert store.count() == 2


def test_upsert_overwrites(store):
    store.save(_entry("x"))
    updated = _entry("x")
    updated["content"] = "We decided to use Redis instead"
    store.save(updated)
    assert store.get("x")["content"] == "We decided to use Redis instead"
    assert store.count() == 1
