# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""P3: the bridge registry behind the graph edge.

The graph edge says which partner may reach which wing today. The registry is
what lets the same question be answered after the partnership ends, which is
why it exists next to the keys rather than only in them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from threadweave import peer as peer_mod

PEER_ID = "nordvik"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A private home and a private registry for every test here."""
    root = tmp_path / "home"
    (root / ".threadweave").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: root)
    monkeypatch.setenv(peer_mod.BRIDGES_ENV, str(root / ".threadweave" / "bridges.json"))
    return root


def _write_keys(root: Path, *, revoked: bool = False) -> None:
    entry = {
        "key": "sk-peer-test", "tenant_id": "default", "role": "readonly",
        "wing": "engineering", "label": f"peer:{PEER_ID}", "peer_id": PEER_ID,
        "peer_contact": "security@example.invalid", "issued_by": "harald",
        "issued_at": "2026-10-10T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00", "max_results": 25,
    }
    if revoked:
        entry["revoked"] = True
        entry["revoked_at"] = "2026-10-11T00:00:00+00:00"
    (root / ".threadweave" / "keys.json").write_text(json.dumps({"keys": [entry]}))


def test_live_bridge_keeps_the_shape_the_graph_and_api_expect(home):
    _write_keys(home)
    edges = peer_mod.bridges()
    assert len(edges) == 1
    edge = edges[0]
    assert edge == {
        "id": f"peer:{PEER_ID}",
        "peer_id": PEER_ID,
        "wing": "engineering",
        "label": "security@example.invalid",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "max_results": 25,
        "bridge": True,
    }


def test_a_closed_bridge_is_kept_and_named(home):
    keys_file = str(home / ".threadweave" / "keys.json")
    _write_keys(home)
    raw, record = peer_mod.issue_peer_key(
        "acme2", "legal", contact="legal@example.invalid", issued_by="harald",
        path=keys_file,
    )
    assert record["peer_id"] == "acme2"
    assert peer_mod.bridges(include_closed=True), "the new bridge must be in the registry"
    assert peer_mod.revoke_peer_key("acme2", keys_file) == 1

    live = peer_mod.bridges()
    assert "acme2" not in [edge["peer_id"] for edge in live], "a closed bridge is not reachable"

    history = {edge["peer_id"]: edge for edge in peer_mod.bridges(include_closed=True)}
    assert history["acme2"]["active"] is False
    assert history["acme2"]["wing"] == "legal"
    assert history["acme2"]["issued_by"] == "harald"
    assert history["acme2"]["closed_at"], "the close time is what an owner asks about later"


def test_a_key_issued_before_the_registry_is_backfilled(home):
    """Keys from before P3 must appear, or the graph would lie by omission."""
    _write_keys(home)
    assert not (home / ".threadweave" / "bridges.json").exists()
    edges = peer_mod.bridges(include_closed=True)
    assert [edge["peer_id"] for edge in edges] == [PEER_ID]
    assert (home / ".threadweave" / "bridges.json").is_file()


def test_a_revoked_key_is_listed_as_a_closed_bridge(home):
    _write_keys(home, revoked=True)
    live = peer_mod.bridges()
    assert live == []
    closed = peer_mod.bridges(include_closed=True)
    assert [edge["peer_id"] for edge in closed] == [PEER_ID]
    assert closed[0]["active"] is False
    assert closed[0]["wing"] == "engineering"


def test_the_registry_survives_a_keys_file_rewrite(home):
    """The registry is ThreadWeave's own metadata, so losing keys.json is not losing the record."""
    _write_keys(home)
    peer_mod.bridges(include_closed=True)
    (home / ".threadweave" / "keys.json").write_text(json.dumps({"keys": []}))
    closed = peer_mod.bridges(include_closed=True)
    assert [edge["peer_id"] for edge in closed] == [PEER_ID]
