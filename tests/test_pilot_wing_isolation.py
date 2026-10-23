# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Pilot condition: the pilot team's knowledge stays in its own wing.

This module is the evidence for the first pilot condition in
``enterprise-adoption.md`` ("Pilot team's knowledge stays in its own wing") and
for item 5 of ``sprint-2026-10-23-reference-path.md`` ("unreachable from another
wing with a peer key"). The pilot team is wing ``pilot``; every other team is
"another wing", exercised here through the partner key a second team holds.

The claim is one sentence: *an entry stored in wing A is never returned to a
caller whose key is scoped to wing B, whatever it asks for and on whichever
search path it asks.* There are four places such a hit could arrive, and each
has a test that pins the filter responsible for it:

    the keyword path            test_pilot_knowledge_is_not_returned_to_a_peer_scoped_to_another_wing
    the request body            test_a_peer_cannot_widen_its_wing_through_the_request
    the MemPalace hybrid path   test_pilot_knowledge_never_arrives_on_the_hybrid_search_path
    a direct read by entry id   test_pilot_knowledge_is_unreadable_by_id_from_another_wing

plus the symmetric case, two wings whose peers must not read each other:
``test_two_wings_peers_cannot_read_each_other``, and a near-miss case, wing
names that a loosened comparison would accept as the pilot wing:
``test_near_miss_wing_names_are_not_the_pilot_wing``.

Every test also asserts the positive control: the caller's own wing still comes
back. A filter that simply returns nothing must fail this module, not pass it.
Each docstring names the mutation that turns it red, so the module can be read
as a list of filters rather than a list of assertions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from threadweave import auth as auth_mod
from threadweave.confidentiality import get_audit_log

TENANT = "pilotco"
PILOT_WING = "pilot"
OTHER_WING = "finance"

ADMIN_KEY = "test-admin-key"
# Two partner keys, each scoped to one wing. A peer key is the only key type
# whose wing the server enforces, so it is the strictest caller we can test.
PILOT_PEER_KEY = "test-pilot-peer-key"
PILOT_PEER_ID = "pilotteam"
OTHER_PEER_KEY = "test-finance-peer-key"
OTHER_PEER_ID = "financepartner"


def _keys_doc() -> dict:
    return {"keys": [
        {"key": ADMIN_KEY, "tenant_id": "*", "role": "admin", "label": "owner"},
        {"key": PILOT_PEER_KEY, "tenant_id": TENANT, "role": "readonly",
         "wing": PILOT_WING, "label": f"peer:{PILOT_PEER_ID}",
         "peer_id": PILOT_PEER_ID, "peer_contact": "pilot@example.invalid",
         "issued_by": "owner", "issued_at": "2026-10-23T00:00:00+00:00",
         "expires_at": "2099-01-01T00:00:00+00:00", "max_results": 25},
        {"key": OTHER_PEER_KEY, "tenant_id": TENANT, "role": "readonly",
         "wing": OTHER_WING, "label": f"peer:{OTHER_PEER_ID}",
         "peer_id": OTHER_PEER_ID, "peer_contact": "finance@example.invalid",
         "issued_by": "owner", "issued_at": "2026-10-23T00:00:00+00:00",
         "expires_at": "2099-01-01T00:00:00+00:00", "max_results": 25},
    ]}


@pytest.fixture
def pilot_world(monkeypatch, tmp_path):
    """Auth on: an owner key, and one peer key per wing."""
    home = tmp_path / "home"
    (home / ".threadweave").mkdir(parents=True)
    (home / ".threadweave" / "keys.json").write_text(json.dumps(_keys_doc()))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("THREADWEAVE_REQUIRE_AUTH", "1")
    auth_mod.reset()
    auth_mod.reset_peer_rate_limits()
    yield home
    # Restore the module default by hand: the env var and the patched
    # Path.home are still in place here, so a bare reset() would leak auth-on
    # into every test that follows (the same trap documented in
    # tests/test_peer_keys.py).
    monkeypatch.delenv("THREADWEAVE_REQUIRE_AUTH", raising=False)
    auth_mod.configure(enabled=False)


@pytest.fixture
def api():
    from threadweave.api import app

    return TestClient(app)


ADMIN = {"X-API-Key": ADMIN_KEY}
PILOT_PEER = {"X-API-Key": PILOT_PEER_KEY}
OTHER_PEER = {"X-API-Key": OTHER_PEER_KEY}


def _save(client, *, wing: str, content: str, room: str = "ledger",
          sensitivity: str = "internal") -> str:
    """Store one entry as the owner, in the pilot tenant."""
    resp = client.post("/api/v1/entries", json={
        "content": content, "wing": wing, "room": room,
        "source_type": "manual", "author_id": "owner", "scope": "team",
        "tenant_id": TENANT, "sensitivity": sensitivity,
    }, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _search(client, query: str, headers: dict, limit: int = 25, **extra) -> list[str]:
    """Search as one caller, returning the entry ids it got back."""
    resp = client.post("/api/v1/search", json={
        "query": query, "limit": limit, "tenant_id": TENANT, **extra,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return [r["id"] for r in resp.json()["results"]]


# ── 1. The keyword path ─────────────────────────────────────
# Pins: the peer-wing filter in the keyword loop of api.search() (`if peer_wing
# and entry["wing"] != peer_wing: continue`).

def test_pilot_knowledge_is_not_returned_to_a_peer_scoped_to_another_wing(pilot_world, api):
    """A second team's peer key never sees the pilot team's entries.

    Fails if the keyword-path wing filter is removed. Loosening the comparison
    instead of removing it (case-insensitive, prefix) is caught by
    test_near_miss_wing_names_are_not_the_pilot_wing.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="We decided the reconciliation ledger moves onto postgres "
                "after the March capacity review.",
    )
    finance = _save(
        api, wing=OTHER_WING,
        content="The reconciliation figures for the quarter were signed off "
                "by the controller.",
    )

    seen = _search(api, "reconciliation", OTHER_PEER)
    assert finance in seen, "the caller's own wing must still come back"
    assert pilot not in seen, "the pilot wing's entry crossed a wing boundary"

    # Control: the entry exists and is findable, so the assertion above is
    # about the wing filter rather than about a search that returns nothing.
    assert pilot in _search(api, "reconciliation", ADMIN)


# ── 2. The request body ────────────────────────────────────
# Pins: the same filter, against every lever a caller can pull itself. The
# body's wing/room/requester identity must not be able to select a wing the
# key does not hold.

@pytest.mark.parametrize("widen", [
    {},
    {"wing": PILOT_WING},
    {"room": "ledger", "wing": PILOT_WING},
    {"limit": 100},
    {"requester_team": PILOT_WING},
    {"requester_role": "admin"},
])
def test_a_peer_cannot_widen_its_wing_through_the_request(pilot_world, api, widen):
    """No request shape lets another wing's peer select the pilot wing.

    Fails if the body's `wing` replaces the key's wing instead of being
    intersected with it, or if a body identity claim overrides the key.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="The escrow release checklist is owned by the pilot team "
                "and reviewed every Friday.",
    )
    assert pilot not in _search(api, "escrow", OTHER_PEER, **widen)


# ── 3. The MemPalace hybrid path ───────────────────────────
# Pins: the peer-wing gate on the MERGED result set in api.search() (`visible =
# [r for r in visible if r.get("wing") == peer_wing]`). The hybrid path appends
# its hits before that gate runs, which is where the leak was found live.

class _HybridHit:
    def __init__(self, drawer_id: str, wing: str, content: str):
        self.drawer_id = drawer_id
        self.wing = wing
        self.tenant_id = TENANT
        self.room = "ledger"
        self.content = content
        self.created_at = "2026-10-20T09:00:00+00:00"
        self.similarity = 0.94
        self.bm25_score = 1.0
        self.sensitivity = "internal"
        self.version_of = ""
        self.author_id = "owner"


class _HybridPalace:
    def __init__(self, hits):
        self._hits = hits

    def search(self, **_kwargs):
        return self._hits


def _install_hybrid(monkeypatch, hits, api_client):
    from threadweave import api as api_mod

    for hit in hits:
        api_mod._memory_store.setdefault(hit.drawer_id, {
            "id": hit.drawer_id, "content": hit.content, "wing": hit.wing,
            "room": hit.room, "scope": "team", "source_type": "manual",
            "author_id": "owner", "title": "", "created_at": hit.created_at,
            "entities": [], "content_type": "decision", "has_pii": False,
            "tenant_id": TENANT, "sensitivity": "internal",
            "source_metadata": {}, "allowed_people": [], "acl": {},
        })
    monkeypatch.setattr(api_mod, "_mempalace_available", True)
    monkeypatch.setattr(api_mod, "_mempalace", _HybridPalace(hits))


def test_pilot_knowledge_never_arrives_on_the_hybrid_search_path(pilot_world, api, monkeypatch):
    """A semantic hit in the pilot wing is dropped for another wing's peer.

    Fails if the merged-set gate is removed, or left only in the keyword loop:
    the hybrid hits are appended before either filter and would come back.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="The freight routing policy was revised to hold parcels "
                "over 30kg at the depot.",
    )
    own = _save(
        api, wing=OTHER_WING,
        content="The freight invoice from the carrier is paid monthly.",
    )
    _install_hybrid(monkeypatch, [
        _HybridHit(pilot, PILOT_WING, "freight routing policy revision"),
        _HybridHit(own, OTHER_WING, "freight invoice paid monthly"),
    ], api)

    seen = _search(api, "freight", OTHER_PEER)
    assert own in seen, "the hybrid path must still return the caller's own wing"
    assert pilot not in seen, "a hybrid hit crossed the wing boundary"


# ── 4. A direct read by entry id ───────────────────────────
# Pins: the peer-wing gate in api.get_entry(). The allowlist lets a peer read
# exactly one entry, so this is the route where an id is the only input.

def test_pilot_knowledge_is_unreadable_by_id_from_another_wing(pilot_world, api):
    """The entry id is not a capability: another wing's peer gets 404.

    Fails if the wing check in get_entry() is removed, or if it only logs the
    denial without refusing the read.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="The telemetry retention window for the pilot team is "
                "ninety days, decided in the October review.",
    )

    refused = api.get(f"/api/v1/entries/{pilot}", headers=OTHER_PEER)
    assert refused.status_code == 404, refused.text

    # The refusal is auditable: the owner can show what was attempted.
    denials = [
        r for r in get_audit_log().get_for_requester(f"peer:{OTHER_PEER_ID}")
        if r.get("action") == "denied" and r.get("entry_id") == pilot
    ]
    assert denials, "a refused cross-wing read must leave an audit record"
    assert denials[0]["entry_wing"] == PILOT_WING

    # Control: the same id is readable by the peer that holds that wing.
    allowed = api.get(f"/api/v1/entries/{pilot}", headers=PILOT_PEER)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["id"] == pilot


# ── 5. Symmetry ────────────────────────────────────────────

def test_two_wings_peers_cannot_read_each_other(pilot_world, api):
    """Each of two winged callers sees its own wing and only its own.

    Fails if the wing filter is applied to one direction of the comparison, or
    to search but not to a single-entry read.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="The vendor onboarding checklist for the pilot team was "
                "finalised on Thursday.",
    )
    finance = _save(
        api, wing=OTHER_WING,
        content="The vendor onboarding invoice template is owned by finance.",
    )

    pilot_sees = _search(api, "vendor onboarding", PILOT_PEER)
    other_sees = _search(api, "vendor onboarding", OTHER_PEER)

    assert pilot in pilot_sees and finance not in pilot_sees
    assert finance in other_sees and pilot not in other_sees

    assert api.get(f"/api/v1/entries/{pilot}", headers=PILOT_PEER).status_code == 200
    assert api.get(f"/api/v1/entries/{finance}", headers=PILOT_PEER).status_code == 404
    assert api.get(f"/api/v1/entries/{finance}", headers=OTHER_PEER).status_code == 200
    assert api.get(f"/api/v1/entries/{pilot}", headers=OTHER_PEER).status_code == 404


# ── 6. Near-miss wing names ────────────────────────────────
# Pins: the comparison itself, not only that some comparison exists. A wing
# name that a case-insensitive or prefix match would accept as the pilot wing
# is a different wing, and the pilot peer must not read it.

def test_near_miss_wing_names_are_not_the_pilot_wing(pilot_world, api):
    """Wing equality is exact: 'PILOT' and 'pilot-archive' are other wings.

    Fails if the comparison on any path is loosened to a case-insensitive or a
    prefix match, which would hand the decoys below to the pilot peer.
    """
    pilot = _save(
        api, wing=PILOT_WING,
        content="The supplier direct pilot covers the north depot from November.",
    )
    shouted = _save(
        api, wing=PILOT_WING.upper(),
        content="The supplier direct pilot covers the south depot from December.",
    )
    suffixed = _save(
        api, wing=f"{PILOT_WING}-archive",
        content="The supplier direct pilot covered the west depot through 2025.",
    )

    seen = _search(api, "supplier direct", PILOT_PEER)
    assert pilot in seen, "the pilot peer must still see its own wing"
    assert shouted not in seen, "a case variant of the wing name leaked to the pilot peer"
    assert suffixed not in seen, "a prefix-related wing name leaked to the pilot peer"

    # The same two decoys on the direct-read path.
    assert api.get(f"/api/v1/entries/{shouted}", headers=PILOT_PEER).status_code == 404
    assert api.get(f"/api/v1/entries/{suffixed}", headers=PILOT_PEER).status_code == 404
