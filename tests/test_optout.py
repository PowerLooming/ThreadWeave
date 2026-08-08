# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the opt-out / right-to-delete layer ("camera sign")."""

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave import optout as optout_mod
from threadweave.optout import OptOutStore

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_optout(tmp_path, monkeypatch):
    """Isolate the opt-out store per test (module-level singleton)."""
    store = OptOutStore(path=str(tmp_path / "optout.json"))
    monkeypatch.setattr(optout_mod, "_store", store)
    return store


# ---- OptOutStore ----

def test_opt_out_and_in_roundtrip():
    store = OptOutStore()
    assert store.opt_out("AdeleV@lqdx.onmicrosoft.com") is True
    assert store.is_opted_out("adelev@lqdx.onmicrosoft.com")  # case-insensitive
    assert store.opt_out("AdeleV@lqdx.onmicrosoft.com") is False  # already out
    assert store.opt_in("ADELEV@lqdx.onmicrosoft.com") is True
    assert not store.is_opted_out("adelev@lqdx.onmicrosoft.com")


def test_optout_store_persists(tmp_path):
    path = str(tmp_path / "o.json")
    s1 = OptOutStore(path=path)
    s1.opt_out("patti@x.com")

    s2 = OptOutStore(path=path)  # new instance reads the file
    assert s2.is_opted_out("patti@x.com")


# ---- API: opt-out endpoints ----

def test_api_optout_endpoints():
    r = client.post("/api/v1/optout/out", json={"person": "Adele@x.com"})
    assert r.status_code == 200
    assert r.json()["opted_out"] is True

    r = client.get("/api/v1/optout")
    assert "adele@x.com" in r.json()["opted_out"]

    r = client.post("/api/v1/optout/in", json={"person": "adele@x.com"})
    assert r.json()["opted_out"] is False


def test_ingest_rejects_opted_out_author():
    client.post("/api/v1/optout/out", json={"person": "adele@x.com"})

    r = client.post("/api/v1/ingest", json={
        "content": "We decided to use PostgreSQL for the auth service",
        "source": "email",
        "tenant_id": "default",
        "metadata": {"email_sender": "adele@x.com"},
    })
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "opted_out"
    assert body["should_save"] is False


def test_ingest_rejects_opted_out_email_participant():
    client.post("/api/v1/optout/out", json={"person": "patti@x.com"})

    r = client.post("/api/v1/ingest", json={
        "content": "We decided to move the CI runner to a self-hosted agent",
        "source": "email",
        "tenant_id": "default",
        "metadata": {"email_sender": "admin@x.com",
                     "email_participants": "adele@x.com,patti@x.com"},
    })
    body = r.json()
    assert body["id"] == "opted_out"


def test_ingest_allows_non_opted_out():
    r = client.post("/api/v1/ingest", json={
        "content": "We decided to use Redis for the session cache",
        "source": "teams",
        "tenant_id": "default",
        "metadata": {"author_id": "someone-else@x.com"},
    })
    body = r.json()
    assert body["id"] != "opted_out"


def test_email_ingest_sets_author_and_author_can_delete():
    """Email captures store author_id from email_sender so the author
    can delete their own entries (regression: was 'unknown')."""
    r = client.post("/api/v1/ingest", json={
        "content": "We resolved the email deletion issue. The root cause was "
                   "the missing author mapping, so we decided to store the "
                   "sender identity on the entry. This fixed self-service "
                   "deletion completely.",
        "source": "email",
        "tenant_id": "default",
        "metadata": {
            "email_sender": "adele@x.com",
            "title": "email deletion fix",
        },
    })
    body = r.json()
    assert body["should_save"] is True
    entry_id = body["id"]

    # the author can delete their own email-derived entry
    r = client.delete(f"/api/v1/entries/{entry_id}",
                      params={"person_id": "adele@x.com", "role": "readwrite"})
    assert r.status_code == 204

    r = client.get(f"/api/v1/entries/{entry_id}",
                   params={"person_id": "adele@x.com", "role": "readwrite"})
    assert r.status_code == 404


# ---- API: delete entry ----

def test_delete_entry_requires_rights():
    # create an entry by author A
    r = client.post("/api/v1/entries", json={
        "content": "Decision about the inventory sync pipeline",
        "source_type": "email",
        "author_id": "adele@x.com",
        "wing": "Retail",
        "room": "decision",
        "tenant_id": "default",
    })
    entry_id = r.json()["id"]

    # someone else (not author, no wing) cannot delete
    r = client.delete(f"/api/v1/entries/{entry_id}",
                      params={"person_id": "stranger@x.com", "role": "readwrite"})
    assert r.status_code == 403

    # author can delete
    r = client.delete(f"/api/v1/entries/{entry_id}",
                      params={"person_id": "adele@x.com", "role": "readwrite"})
    assert r.status_code == 204

    # gone
    r = client.get(f"/api/v1/entries/{entry_id}",
                   params={"person_id": "adele@x.com", "role": "readwrite"})
    assert r.status_code == 404


def test_delete_entry_admin_bypasses():
    r = client.post("/api/v1/entries", json={
        "content": "Confidential budget discussion for Q3 planning cycle",
        "source_type": "teams",
        "author_id": "adele@x.com",
        "wing": "Retail",
        "room": "decision",
        "tenant_id": "default",
    })
    entry_id = r.json()["id"]

    r = client.delete(f"/api/v1/entries/{entry_id}",
                      params={"role": "admin"})
    assert r.status_code == 204
