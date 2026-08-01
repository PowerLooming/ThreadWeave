# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for MemPalaceClient search metadata round-tripping.

Regression coverage for:
- Drawer IDs surviving BM25 re-ranking (they were lost, collapsing
  semantic search to at most one result with an empty id)
- tenant_id / sensitivity stored on write and returned on search
"""

import pytest

from threadweave.mempalace_client import MemPalaceClient


@pytest.fixture
def mp_client(tmp_path):
    return MemPalaceClient(palace_path=str(tmp_path / "palace"))


@pytest.fixture
def populated_client(mp_client):
    """A client with three drawers sharing tenant/sensitivity metadata."""
    drawers = [
        ("d1", "We decided to use PostgreSQL for the auth service because of JSONB support",
         "engineering", "database", "tenant-a", "internal"),
        ("d2", "The API gateway should use OAuth2 with PKCE for all internal services",
         "engineering", "api", "tenant-a", "confidential"),
        ("d3", "Quarterly revenue target for the marine division is 12% growth",
         "finance", "planning", "tenant-b", "restricted"),
    ]
    for did, content, wing, room, tenant, sens in drawers:
        assert mp_client.add_drawer(
            content=content, wing=wing, room=room,
            drawer_id=did, tenant_id=tenant, sensitivity=sens,
        ) == did
    return mp_client


def test_search_preserves_drawer_ids(populated_client):
    """All results must come back with their original, non-empty drawer ids."""
    results = populated_client.search("PostgreSQL auth service", limit=5)
    assert len(results) >= 1
    ids = [r.drawer_id for r in results]
    assert all(ids), f"Empty drawer_id in results: {ids}"
    assert len(ids) == len(set(ids)), f"Duplicate drawer_ids: {ids}"
    assert "d1" in ids


def test_search_returns_tenant_and_sensitivity(populated_client):
    """tenant_id and sensitivity must round-trip through search."""
    results = populated_client.search("revenue target", limit=5)
    hit = next((r for r in results if r.drawer_id == "d3"), None)
    assert hit is not None
    assert hit.tenant_id == "tenant-b"
    assert hit.sensitivity == "restricted"

    # Legacy drawers without the metadata fall back to empty strings,
    # which callers treat as "internal"/untagged.
    hit_legacy = next((r for r in results if r.drawer_id == "d1"), None)
    assert hit_legacy is not None
    assert hit_legacy.tenant_id == "tenant-a"
    assert hit_legacy.sensitivity == "internal"


def test_add_drawer_generates_id_when_not_provided(mp_client):
    """Without an explicit drawer_id, add_drawer still returns a usable id."""
    did = mp_client.add_drawer(
        content="Always use connection pooling with at least 20 connections",
        wing="engineering", room="database",
    )
    assert did, "add_drawer returned no id"
    results = mp_client.search("connection pooling", limit=5)
    assert any(r.drawer_id == did for r in results)
