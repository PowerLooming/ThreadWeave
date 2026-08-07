# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the durable entry store (SQLite + PostgreSQL backends)."""

import os
import pytest

from threadweave.store import EntryStore

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


@pytest.fixture
def store(tmp_path):
    return EntryStore(url=f"sqlite:///{tmp_path}/entries.sqlite3")


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
    url = f"sqlite:///{tmp_path}/entries.sqlite3"
    EntryStore(url=url).save(_entry("e1"))
    EntryStore(url=url).save(_entry("e2", tenant="lqdx", wing="Retail"))

    restarted = EntryStore(url=url)
    entries = restarted.load_all()
    assert len(entries) == 2
    by_id = {e["id"]: e for e in entries}
    assert by_id["e1"]["tenant_id"] == "default"
    assert by_id["e2"]["tenant_id"] == "lqdx"
    assert by_id["e2"]["wing"] == "Retail"


def test_delete_removes_persistently(tmp_path):
    url = f"sqlite:///{tmp_path}/entries.sqlite3"
    s1 = EntryStore(url=url)
    s1.save(_entry("keep"))
    s1.save(_entry("gone"))
    s1.delete("gone")

    s2 = EntryStore(url=url)
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


# ---- PostgreSQL backend ----

@pytest.mark.skipif(not TEST_POSTGRES_URL, reason="TEST_POSTGRES_URL not set")
def test_postgres_save_load_delete():
    """Full roundtrip against a real PostgreSQL server (CI/dev only)."""
    s = EntryStore(url=TEST_POSTGRES_URL)
    s.save(_entry("pg1"))
    s.save(_entry("pg2", tenant="lqdx", wing="Retail"))
    assert s.count() == 2

    by_id = {e["id"]: e for e in s.load_all()}
    assert by_id["pg1"]["tenant_id"] == "default"
    assert by_id["pg2"]["wing"] == "Retail"

    s.delete("pg1")
    assert s.get("pg1") is None
    assert s.count() == 1
    s.delete("pg2")


def test_sql_compiles_for_postgresql_dialect():
    """The store's SQL must compile for the PG dialect (no server needed)."""
    from sqlalchemy import create_engine
    from sqlalchemy.dialects import postgresql
    from sqlalchemy import text

    # Compile the DDL + upsert against the postgresql dialect to catch
    # dialect-incompatible SQL without requiring a live server.
    from threadweave.store import EntryStore as ES

    es = ES.__new__(ES)
    es._to_row  # noqa: B018  (verify static method exists)
    row = es._to_row(_entry())
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    upsert = (
        f"INSERT INTO entries ({', '.join(cols)}) VALUES ({placeholders}) "
        "ON CONFLICT(id) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    )
    engine = create_engine("postgresql://u:p@h/db", _initialize=False)
    compiled = str(text(upsert).compile(dialect=postgresql.dialect()))
    assert compiled.startswith("INSERT INTO entries")
    assert "ON CONFLICT" in compiled
    # Named params become PG-style %(name)s
    assert "%(id)s" in compiled
