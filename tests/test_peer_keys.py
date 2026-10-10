# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""P1 peer keys: the allowlist, the wing, the cap and the audit trail.

Covers the ten tests in design-peer-sharing.md section 10.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from threadweave import auth as auth_mod
from threadweave import peer as peer_mod
from threadweave.auth import OPEN_PATHS, peer_allowlist, peer_route_allowed
from threadweave.confidentiality import get_audit_log

PEER_KEY = "sk-peer-nordvik-p1"
TENANT_KEY = "sk-acme-key"
PEER_ID = "nordvik"


def _keys_doc(max_results: int = 2, expires_at: str = "2099-01-01T00:00:00+00:00") -> dict:
    return {"keys": [
        {"key": TENANT_KEY, "tenant_id": "acme", "role": "readwrite",
         "wing": "engineering", "person_id": "alice"},
        {"key": PEER_KEY, "tenant_id": "acme", "role": "readonly",
         "wing": "engineering", "person_id": "",
         "label": f"peer:{PEER_ID}", "peer_id": PEER_ID,
         "peer_contact": "security@example.invalid", "issued_by": "harald",
         "issued_at": "2026-10-10T00:00:00+00:00", "expires_at": expires_at,
         "max_results": max_results},
    ]}


@pytest.fixture
def peer_world(monkeypatch, tmp_path):
    """Auth on, one tenant key and one peer key, both loaded from keys.json."""
    home = tmp_path / "home"
    (home / ".threadweave").mkdir(parents=True)
    (home / ".threadweave" / "keys.json").write_text(json.dumps(_keys_doc()))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("THREADWEAVE_REQUIRE_AUTH", "1")
    auth_mod.reset()
    auth_mod.reset_peer_rate_limits()
    yield home
    # Restore the module default BY HAND. At this point the env var and the
    # patched Path.home are still in place (monkeypatch undoes them after
    # this fixture), so a bare reset() would leave auth ON and this keys.json
    # loaded for every test that follows. That leak turned 10 unrelated tests
    # red in the full run, and they passed in isolation.
    monkeypatch.delenv("THREADWEAVE_REQUIRE_AUTH", raising=False)
    auth_mod.configure(enabled=False)


@pytest.fixture
def peer_world_expired(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".threadweave").mkdir(parents=True)
    (home / ".threadweave" / "keys.json").write_text(
        json.dumps(_keys_doc(expires_at="2026-01-01T00:00:00+00:00"))
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("THREADWEAVE_REQUIRE_AUTH", "1")
    auth_mod.reset()
    auth_mod.reset_peer_rate_limits()
    yield home
    # Restore the module default BY HAND. At this point the env var and the
    # patched Path.home are still in place (monkeypatch undoes them after
    # this fixture), so a bare reset() would leave auth ON and this keys.json
    # loaded for every test that follows. That leak turned 10 unrelated tests
    # red in the full run, and they passed in isolation.
    monkeypatch.delenv("THREADWEAVE_REQUIRE_AUTH", raising=False)
    auth_mod.configure(enabled=False)


@pytest.fixture
def api():
    from threadweave.api import app
    return TestClient(app)


PEER_HEADERS = {"X-API-Key": PEER_KEY}
TENANT_HEADERS = {"X-API-Key": TENANT_KEY}


def _save(client, *, wing: str, content: str, room: str = "architecture",
          sensitivity: str = "internal") -> str:
    resp = client.post("/api/v1/entries", json={
        "content": content, "wing": wing, "room": room,
        "source_type": "manual", "author_id": "alice", "scope": "team",
        "tenant_id": "acme", "sensitivity": sensitivity,
    }, headers=TENANT_HEADERS)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _search(client, query: str = "postgres", limit: int = 10, **extra):
    return client.post("/api/v1/search",
                       json={"query": query, "limit": limit, "tenant_id": "acme", **extra},
                       headers=PEER_HEADERS)


# ── 1. The allowlist is two routes, and nothing else ─────────

def test_allowlist_is_search_and_one_entry():
    assert peer_allowlist() == ["POST /api/v1/search", "GET /api/v1/entries/{entry_id}"]


def test_allowlist_matching():
    assert peer_route_allowed("POST", "/api/v1/search")
    assert peer_route_allowed("GET", "/api/v1/entries/abc123")
    assert not peer_route_allowed("GET", "/api/v1/entries")
    assert not peer_route_allowed("GET", "/api/v1/entries/abc123/versions")
    assert not peer_route_allowed("GET", "/api/v1/entries/abc123/refine")
    assert not peer_route_allowed("POST", "/api/v1/entries")
    assert not peer_route_allowed("GET", "/api/v1/tenants/acme/entries")
    assert not peer_route_allowed("DELETE", "/api/v1/entries/abc123")


def test_every_other_route_refuses_a_peer_key(peer_world, api):
    """Structural: walk the app and assert the refusal holds everywhere."""
    from threadweave.api import app

    allowed_paths = {"/api/v1/search", "/api/v1/entries/{entry_id}"}
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        if not path.startswith("/api/v1") or path in OPEN_PATHS:
            continue
        if path in allowed_paths:
            continue
        method = "GET" if "GET" in methods else sorted(methods)[0]
        concrete = path.replace("{entry_id}", "zzz").replace("{tenant_id}", "acme") \
                       .replace("{requester_id}", "x").replace("{entry_id:path}", "zzz")
        resp = api.request(method, concrete, headers=PEER_HEADERS)
        assert resp.status_code == 403, f"{method} {concrete} -> {resp.status_code}"
        assert "may only search" in resp.json().get("error", "")
        checked += 1
    assert checked >= 15, f"only {checked} routes walked"


# ── 2. The wing ──────────────────────────────────────────────

def test_peer_searches_its_own_wing_only(peer_world, api):
    mine = _save(api, wing="engineering", content="We chose postgres for the ledger service.")
    theirs = _save(api, wing="legal", content="postgres licensing terms were reviewed by counsel.")
    resp = _search(api, "postgres")
    assert resp.status_code == 200, resp.text
    ids = [r["id"] for r in resp.json()["results"]]
    assert mine in ids
    assert theirs not in ids


def test_peer_reads_a_single_entry_in_its_wing(peer_world, api):
    mine = _save(api, wing="engineering", content="The deploy pipeline uses postgres migrations.")
    theirs = _save(api, wing="legal", content="postgres vendor contract signed by legal.")
    ok = api.get(f"/api/v1/entries/{mine}", headers=PEER_HEADERS)
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == mine
    refused = api.get(f"/api/v1/entries/{theirs}", headers=PEER_HEADERS)
    assert refused.status_code in (403, 404)


def test_peer_cannot_widen_its_wing_through_the_body(peer_world, api):
    theirs = _save(api, wing="legal", content="postgres vendor contract signed by legal.")
    resp = _search(api, "postgres", wing="legal")
    assert resp.status_code == 200
    assert theirs not in [r["id"] for r in resp.json()["results"]]


# ── 3. The cap ───────────────────────────────────────────────

def test_peer_search_is_capped(peer_world, api):
    for i in range(5):
        _save(api, wing="engineering", content=f"postgres note {i} about the ledger.")
    resp = _search(api, "postgres", limit=50)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) <= 2


# ── 4. Expiry and revocation ─────────────────────────────────

def test_expired_peer_key_is_refused(peer_world_expired, api):
    resp = _search(api, "postgres")
    assert resp.status_code == 403
    assert "expired" in resp.json().get("error", "").lower()


def test_revoked_peer_key_is_refused_on_the_next_request(peer_world, api):
    keys_file = peer_world / ".threadweave" / "keys.json"
    assert _search(api, "postgres").status_code in (200, 403)
    assert peer_mod.revoke_peer_key(PEER_ID, str(keys_file)) == 1
    auth_mod.reset()
    resp = _search(api, "postgres")
    assert resp.status_code == 403
    assert "invalid api key" in resp.json().get("error", "").lower()


# ── 5. The audit trail ───────────────────────────────────────

def test_peer_search_is_audited_without_the_query_text(peer_world, api):
    _save(api, wing="engineering", content="postgres was chosen for the ledger.")
    _search(api, "postgres")
    records = get_audit_log().get_for_requester(f"peer:{PEER_ID}")
    searches = [r for r in records if r.get("action") == "peer_search"]
    assert searches, records
    assert "query_sha256=" in searches[0]["reason"]
    assert "postgres" not in json.dumps(searches[0])


def test_peer_entry_read_is_audited(peer_world, api):
    entry_id = _save(api, wing="engineering", content="postgres migrations run on deploy.")
    api.get(f"/api/v1/entries/{entry_id}", headers=PEER_HEADERS)
    records = get_audit_log().get_for_requester(f"peer:{PEER_ID}")
    reads = [r for r in records if r.get("action") == "peer_read" and r.get("entry_id") == entry_id]
    assert reads, records
    assert reads[0]["entry_wing"] == "engineering"


# ── 6. Issuance, listing, revocation as files ────────────────

def test_issue_list_revoke_roundtrip(tmp_path):
    keys_file = str(tmp_path / "keys.json")
    raw, record = peer_mod.issue_peer_key(
        PEER_ID, "engineering", contact="security@example.invalid",
        issued_by="harald", path=keys_file,
    )
    assert raw.startswith(peer_mod.KEY_PREFIX)
    assert record["role"] == "readonly"
    assert record["max_results"] == peer_mod.DEFAULT_MAX_RESULTS
    listed = peer_mod.list_peers(keys_file)
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert raw not in json.dumps(listed)
    with pytest.raises(ValueError):
        peer_mod.issue_peer_key(PEER_ID, "legal", path=keys_file)
    assert peer_mod.revoke_peer_key(PEER_ID, keys_file) == 1
    assert peer_mod.list_peers(keys_file)[0]["revoked"] is True
    assert peer_mod.revoke_peer_key(PEER_ID, keys_file) == 0


def test_peer_key_never_grants_write():
    for method, path in [("POST", "/api/v1/entries"), ("DELETE", "/api/v1/entries/x"),
                         ("POST", "/api/v1/ingest"), ("POST", "/api/v1/tenants/acme/keys")]:
        assert not peer_route_allowed(method, path)


# ── 7. The MemPalace hybrid path is gated too ────────────────
# Found by running the live server, not by the tests above: the hybrid
# search path appended hits before the keyword path's wing filter ran, so
# another wing's entry came back. This closes that path with a fake
# MemPalace that returns exactly one hit in the wrong wing.

class _FakeHit:
    def __init__(self, drawer_id: str, wing: str, tenant_id: str = "acme"):
        self.drawer_id = drawer_id
        self.wing = wing
        self.tenant_id = tenant_id
        self.room = "contracts"
        self.content = "postgres vendor contract signed by legal."
        self.created_at = "2026-10-01T00:00:00"
        self.similarity = 0.93
        self.bm25_score = 1.0
        self.sensitivity = "internal"
        self.version_of = ""


class _FakeMemPalace:
    def __init__(self, hits):
        self._hits = hits

    def search(self, **_kwargs):
        return self._hits


def _install_fake_hybrid(monkeypatch, hits):
    from threadweave import api as api_mod

    monkeypatch.setattr(api_mod, "_mempalace_available", True)
    monkeypatch.setattr(api_mod, "_mempalace", _FakeMemPalace(hits))


def test_hybrid_hits_in_another_wing_never_reach_a_peer(peer_world, api, monkeypatch):
    legal = _save(api, wing="legal", content="postgres vendor contract signed by legal.")
    _install_fake_hybrid(monkeypatch, [_FakeHit(legal, "legal")])

    peer = _search(api, "postgres")
    assert peer.status_code == 200
    assert legal not in [r["id"] for r in peer.json()["results"]]

    # The same fake hit does reach an ordinary tenant key: the gate is
    # peer-specific, not a search that returns nothing at all.
    tenant = api.post("/api/v1/search",
                      json={"query": "postgres", "limit": 10, "tenant_id": "acme"},
                      headers=TENANT_HEADERS)
    assert tenant.status_code == 200
    assert legal in [r["id"] for r in tenant.json()["results"]]


def test_hybrid_hits_in_the_peer_wing_do_reach_it(peer_world, api, monkeypatch):
    mine = _save(api, wing="engineering", content="postgres migrations run on deploy.")
    _install_fake_hybrid(monkeypatch, [_FakeHit(mine, "engineering")])
    resp = _search(api, "postgres")
    assert resp.status_code == 200
    assert mine in [r["id"] for r in resp.json()["results"]]


# ── 8. The MCP seam gets the same allowlist ──────────────────
# The MCP toolset is broader than the HTTP allowlist (it includes a write
# tool and the breadth tools), so a peer key must be held to search and a
# single entry read there too, or the REST gate is decorative.

def _jsonrpc(tool: str) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }).encode()


def test_mcp_peer_tool_allowlist():
    from threadweave.mcp_server import peer_tool_allowed

    assert peer_tool_allowed("search_memory")
    assert peer_tool_allowed("get_entry")
    for tool in ["capture_knowledge", "browse_topics", "who_knows",
                 "list_open_commitments", "classify_text", "get_decisions",
                 "entry_provenance", ""]:
        assert not peer_tool_allowed(tool)


def test_mcp_peer_tool_refusal_reads_the_message():
    from threadweave.mcp_server import peer_tool_refusal

    assert peer_tool_refusal(_jsonrpc("capture_knowledge")) == "capture_knowledge"
    assert peer_tool_refusal(_jsonrpc("search_memory")) == ""
    assert peer_tool_refusal(_jsonrpc("get_entry")) == ""
    assert peer_tool_refusal(b"") == ""
    assert peer_tool_refusal(b"not json") == ""
    handshake = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}}).encode()
    assert peer_tool_refusal(handshake) == ""
    assert peer_tool_refusal(json.dumps({"jsonrpc": "2.0", "id": 2,
                                         "method": "tools/list", "params": {}}).encode()) == ""
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_entry"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "browse_topics"}},
    ]).encode()
    assert peer_tool_refusal(batch) == "browse_topics"


def _mcp_request(body: bytes) -> "object":
    from starlette.requests import Request

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "path": "/mcp",
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"x-api-key", PEER_KEY.encode()), (b"content-type", b"application/json")],
        "server": ("127.0.0.1", 8099), "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_mcp_middleware_refuses_a_peer_tool_and_replays_the_body(peer_world):
    import asyncio

    from starlette.responses import JSONResponse
    from threadweave.mcp_server import MCPAuthMiddleware

    seen: dict = {}

    async def call_next(request):
        seen["body"] = await request.body()
        return JSONResponse({"ok": True})

    middleware = MCPAuthMiddleware(app=None)

    refused = asyncio.run(middleware.dispatch(_mcp_request(_jsonrpc("capture_knowledge")), call_next))
    assert refused.status_code == 403
    assert "may only search" in json.loads(refused.body)["error"]
    assert seen == {}, "a refused call must not reach the tool"

    allowed = asyncio.run(middleware.dispatch(_mcp_request(_jsonrpc("search_memory")), call_next))
    assert allowed.status_code == 200
    assert json.loads(seen["body"])["params"]["name"] == "search_memory"


def test_revocation_reaches_a_running_server_without_a_restart(peer_world, api):
    """The key file is re-read when it changes, so a revoke is immediate.

    Found by the P2 acceptance run: the running server had loaded keys.json
    once at startup, so a revoked key kept working until a restart and the
    claim "revocation ends access on the next request" was false.
    """
    keys_file = Path(peer_world) / ".threadweave" / "keys.json"
    assert _search(api, "postgres").status_code == 200
    assert peer_mod.revoke_peer_key(PEER_ID, str(keys_file)) == 1
    # Deliberately no auth_mod.reset() here: the server keeps running.
    resp = _search(api, "postgres")
    assert resp.status_code == 403
    assert "invalid api key" in resp.json()["error"].lower()
