# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the single-user (personal) runtime profile.

Personal mode is a config value, not a fork: same store, search, LLM and
privacy contract. These tests cover the two seams it touches — the requester
context in the API and the daemon registry filter.
"""

import pytest

from threadweave.profile import (
    DEFAULT_OWNER_ID, DEFAULT_PROFILE, PROFILES, get_owner_id, get_profile,
    is_personal,
)
from threadweave.daemons import (
    DAEMONS, daemon_available, daemons_for_profile, run_daemon,
)
from threadweave.confidentiality import (
    RequesterContext, SensitivityLevel,
)
from threadweave.api import _requester_from_request


# ---- profile.py ----


def test_default_profile_is_org(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_PROFILE", raising=False)
    assert get_profile() == "org"
    assert not is_personal()


def test_personal_profile(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    assert get_profile() == "personal"
    assert is_personal()


def test_invalid_profile_falls_back_to_org(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "corporate")
    assert get_profile() == DEFAULT_PROFILE
    assert not is_personal()


def test_profile_case_insensitive(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "PERSONAL")
    assert is_personal()


def test_owner_id_default_and_override(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_OWNER_ID", raising=False)
    assert get_owner_id() == DEFAULT_OWNER_ID
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "harald@example.com")
    assert get_owner_id() == "harald@example.com"


# ---- daemon registry filter ----


def test_org_profile_exposes_full_registry(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_PROFILE", raising=False)
    assert set(daemons_for_profile()) == set(DAEMONS)


def test_personal_profile_exposes_only_owner_scoped(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    names = set(daemons_for_profile())
    # Owner-scoped capture connector present; org-wide harvesters hidden.
    assert "email-watch" in names
    for org_only in ("teams-watch", "teams-bot", "sharepoint-watch",
                     "graph-daemon", "org-sync"):
        assert org_only not in names


def test_daemon_available_respects_profile(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    assert daemon_available("email-watch")
    assert not daemon_available("teams-watch")


def test_run_daemon_refuses_org_daemon_in_personal(monkeypatch, capsys):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    # teams-watch would actually try to start; ensure it's refused first.
    rc = run_daemon("teams-watch")
    out = capsys.readouterr().out
    assert rc == 2
    assert "not available in the 'personal' profile" in out


# ---- API requester context ----


def test_org_mode_uses_key_claims(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_PROFILE", raising=False)

    class State:
        auth_role = "readwrite"
        auth_person = "alice"
        auth_wing = "eng"
        auth_groups = []

    class Request:
        state = State()

    ctx = _requester_from_request(Request())
    assert ctx.person_id == "alice"
    assert ctx.role == "readwrite"


def test_personal_mode_always_owner_admin(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "harald")
    # Personal branch ignores key/body claims entirely; pass an empty request.
    ctx = _requester_from_request(None, person_id="alice", role="readonly")
    assert ctx.person_id == "harald"
    assert ctx.role == "admin"
    assert ctx.clearance == SensitivityLevel.LEGAL_PRIVILEGED
    assert ctx.groups == []


# ---- owner sees everything in personal mode ----


@pytest.mark.parametrize("sensitivity", [
    SensitivityLevel.INTERNAL,
    SensitivityLevel.CONFIDENTIAL,
    SensitivityLevel.RESTRICTED,
    SensitivityLevel.CLIENT_CONFIDENTIAL,
    SensitivityLevel.HR_PRIVILEGED,
    SensitivityLevel.LEGAL_PRIVILEGED,
])
def test_owner_sees_all_sensitivities_in_personal(monkeypatch, sensitivity):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "harald")
    ctx = _requester_from_request(None)
    entry = {"sensitivity": sensitivity.value, "wing": "hr",
             "client_id": "x", "allowed_people": []}
    assert ctx.can_see(entry, sensitivity) is True


def test_owner_sees_private_channel_entry_in_personal(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "harald")
    ctx = _requester_from_request(None)
    # In personal mode a private-channel entry's allowed_people is [owner].
    entry = {
        "source_metadata": {"private_channel": True},
        "allowed_people": ["harald"],
    }
    assert ctx.can_see(entry) is True


def test_private_channel_still_denies_non_owner(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    ctx = RequesterContext(person_id="other", role="admin")
    entry = {
        "source_metadata": {"private_channel": True},
        "allowed_people": ["harald"],
    }
    assert ctx.can_see(entry) is False


# ---- personal-mode ACL: the owner is the only reader ----


def test_personal_acl_resolves_grants_to_the_owner(monkeypatch):
    """A group grant the owner cannot satisfy must not hide their own capture."""
    from threadweave.api import _personal_acl

    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "owner@example.com")
    acl = {
        "allowed_groups": ["22222222-3333-4444-5555-666666666666"],
        "allowed_users": [],
        "deny_users": ["someone-else"],
    }
    out = _personal_acl(acl)
    assert out["allowed_users"] == ["owner@example.com"]
    assert "allowed_groups" not in out
    assert out["deny_users"] == ["someone-else"]   # deny stays authoritative
    assert acl["allowed_groups"] == [              # input not mutated
        "22222222-3333-4444-5555-666666666666"
    ]


def test_personal_acl_keeps_an_existing_owner_grant(monkeypatch):
    from threadweave.api import _personal_acl

    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "owner@example.com")
    out = _personal_acl({"allowed_users": ["owner@example.com"]})
    assert out["allowed_users"] == ["owner@example.com"]


def test_org_mode_leaves_the_acl_untouched(monkeypatch):
    from threadweave.api import _personal_acl

    monkeypatch.setenv("THREADWEAVE_PROFILE", "org")
    acl = {"allowed_groups": ["g1"]}
    assert _personal_acl(acl) == acl


def test_empty_acl_is_not_stamped(monkeypatch):
    """No source ACL keeps the normal clearance path exactly as before."""
    from threadweave.api import _personal_acl

    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    assert _personal_acl({}) == {}


def test_owner_can_read_a_group_gated_capture_in_personal_mode(monkeypatch):
    """End to end: ingest with a group ACL, read it back as the owner."""
    from fastapi.testclient import TestClient

    from threadweave.api import _memory_store, app

    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.setenv("THREADWEAVE_OWNER_ID", "owner@example.com")
    client = TestClient(app)

    resp = client.post("/api/v1/ingest", json={
        "content": (
            "We have decided to standardise the group-only procurement "
            "ritual and this decision is now finalized and documented."
        ),
        "source": "teams",
        "tenant_id": "personal",
        "metadata": {
            "wing": "procurement", "room": "contracts",
            "sensitivity": "confidential",
            "acl": {"allowed_groups": ["22222222-3333-4444-5555-666666666666"]},
        },
    })
    assert resp.status_code == 201
    eid = resp.json()["id"]

    stored = _memory_store[eid]
    assert stored["acl"]["allowed_users"] == ["owner@example.com"]
    assert "allowed_groups" not in stored["acl"]

    got = client.get(f"/api/v1/entries/{eid}?person_id=owner@example.com")
    assert got.status_code == 200
