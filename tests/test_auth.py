# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for API key authentication middleware.
"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from threadweave.auth import (
    KeyStore,
    configure,
    reset as auth_reset,
)
from threadweave import auth as auth_mod


# ── KeyStore ────────────────────────────────────────────────

class TestKeyStore:
    def test_validate_valid_key(self):
        ks = KeyStore()
        ks.add("sk-abc123", "acme-corp", "readwrite")
        info = ks.validate("sk-abc123")
        assert info is not None
        assert info.tenant_id == "acme-corp"
        assert info.role == "readwrite"

    def test_validate_invalid_key(self):
        ks = KeyStore()
        assert ks.validate("nonexistent") is None

    def test_load_from_env(self):
        import os
        old = os.environ.get("THREADWEAVE_API_KEYS")
        os.environ["THREADWEAVE_API_KEYS"] = "acme:sk-acme,beta:sk-beta"
        try:
            ks = KeyStore()
            assert ks.count >= 2
            assert ks.validate("sk-acme") is not None
            assert ks.validate("sk-beta") is not None
        finally:
            if old is None:
                os.environ.pop("THREADWEAVE_API_KEYS", None)
            else:
                os.environ["THREADWEAVE_API_KEYS"] = old

    def test_load_from_env_empty(self):
        ks = KeyStore()
        # Default KeyStore loads from env, which may or may not have keys.
        # We only assert it doesn't crash.
        assert isinstance(ks.count, int)

    def test_admin_key(self):
        ks = KeyStore()
        ks.add("sk-admin", "*", "admin")
        info = ks.validate("sk-admin")
        assert info.tenant_id == "*"
        assert info.role == "admin"

    def test_load_from_file_with_identity(self, monkeypatch, tmp_path):
        """keys.json entries carry role, wing, and person_id."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        keys_dir = tmp_path / ".threadweave"
        keys_dir.mkdir()
        (keys_dir / "keys.json").write_text(json.dumps({"keys": [
            {"key": "sk-alice", "tenant_id": "acme", "role": "readwrite",
             "wing": "engineering", "person_id": "alice"},
            {"key": "sk-legal", "tenant_id": "*", "role": "legal",
             "wing": "legal", "person_id": "lars"},
        ]}))
        ks = KeyStore()
        alice = ks.validate("sk-alice")
        assert alice is not None
        assert alice.tenant_id == "acme"
        assert alice.role == "readwrite"
        assert alice.wing == "engineering"
        assert alice.person_id == "alice"
        legal = ks.validate("sk-legal")
        assert legal.role == "legal"
        assert legal.wing == "legal"


# ── Middleware: auth disabled (default) ──────────────────────

class TestMiddlewareAuthDisabled:
    @pytest.fixture(autouse=True)
    def _setup(self):
        configure(enabled=False)
        yield
        auth_reset()

    def _client(self):
        from threadweave.api import app
        return TestClient(app)

    def test_ingest_no_key_works(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "Important decision: we will use GraphQL for the API. "
                       "We evaluated gRPC and REST alternatives.",
            "source": "teams",
        })
        assert resp.status_code == 201
        assert resp.json()["should_save"] is True

    def test_health_always_open(self):
        client = self._client()
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_metrics_always_open(self):
        client = self._client()
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200


# ── Middleware: auth enabled ─────────────────────────────────

class TestMiddlewareAuthEnabled:
    @pytest.fixture(autouse=True)
    def _setup(self):
        configure(enabled=True, keys_env="acme:sk-acme-key,beta:sk-beta-key")
        yield
        auth_reset()

    def _client(self):
        from threadweave.api import app
        return TestClient(app)

    def test_missing_key_returns_401(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "Some content",
            "source": "teams",
        })
        assert resp.status_code == 401

    def test_invalid_key_returns_403(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "Some content",
            "source": "teams",
        }, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 403

    def test_valid_key_works(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "Important decision: we will use GraphQL for the API.",
            "source": "teams",
        }, headers={"X-API-Key": "sk-acme-key"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["should_save"] is True

    def test_bearer_token_works(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "We evaluated three database options and chose Postgres.",
            "source": "slack",
        }, headers={"Authorization": "Bearer sk-acme-key"})
        assert resp.status_code == 201

    def test_tenant_scoping(self):
        client = self._client()
        resp = client.post("/api/v1/ingest", json={
            "content": "Some knowledge that belongs to acme not evil-corp.",
            "source": "manual",
            "tenant_id": "evil-corp",
        }, headers={"X-API-Key": "sk-acme-key"})
        assert resp.status_code == 201

        list_resp = client.get(
            "/api/v1/tenants/acme/entries",
            headers={"X-API-Key": "sk-acme-key"},
        )
        assert list_resp.status_code == 200

    def test_health_still_open(self):
        client = self._client()
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_metrics_still_open(self):
        client = self._client()
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200

    def test_detect_endpoint_protected(self):
        client = self._client()
        resp = client.post("/api/v1/detect", json={
            "text": "Some text to classify.",
        })
        assert resp.status_code == 401


# ── Key identity + read-path tenant scoping ──────────────────

class TestMiddlewareKeyIdentity:
    """With auth enabled, requester identity comes from the key and all
    read endpoints are scoped to the key's tenant."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        keys_dir = tmp_path / ".threadweave"
        keys_dir.mkdir()
        (keys_dir / "keys.json").write_text(json.dumps({"keys": [
            {"key": "sk-admin", "tenant_id": "*", "role": "admin"},
            {"key": "sk-hr", "tenant_id": "*", "role": "hr_admin"},
            {"key": "sk-acme", "tenant_id": "acme", "role": "readwrite",
             "wing": "engineering", "person_id": "alice"},
            {"key": "sk-beta", "tenant_id": "beta", "role": "readwrite"},
        ]}))
        old = os.environ.get("THREADWEAVE_API_KEYS")
        os.environ.pop("THREADWEAVE_API_KEYS", None)
        configure(enabled=True)
        yield
        if old is not None:
            os.environ["THREADWEAVE_API_KEYS"] = old
        auth_reset()

    def _client(self):
        from threadweave.api import app
        return TestClient(app)

    def _save(self, client, content, wing, room, tenant_id, key,
              sensitivity=None):
        body = {
            "content": content,
            "wing": wing,
            "room": room,
            "tenant_id": tenant_id,
        }
        if sensitivity:
            body["sensitivity"] = sensitivity
        resp = client.post("/api/v1/entries", json=body,
                           headers={"X-API-Key": key})
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_key_file_identity_loaded(self):
        ks = KeyStore()
        info = ks.validate("sk-acme")
        assert info.tenant_id == "acme"
        assert info.role == "readwrite"
        assert info.wing == "engineering"
        assert info.person_id == "alice"

    def test_search_scoped_to_key_tenant(self):
        client = self._client()
        entry_id = self._save(
            client,
            "Acme specific knowledge: we run three Kubernetes clusters for the radar platform.",
            "engineering", "infra", "evil-corp", "sk-acme",
        )  # body tenant is ignored, entry lands in acme

        # acme key searching with a foreign tenant in the body finds its own entry
        r = client.post("/api/v1/search", json={
            "query": "Kubernetes radar", "tenant_id": "evil-corp",
        }, headers={"X-API-Key": "sk-acme"})
        assert r.status_code == 200
        assert any(x["id"] == entry_id for x in r.json()["results"])

        # beta key must NOT see acme's entry, even when asking for tenant acme
        r = client.post("/api/v1/search", json={
            "query": "Kubernetes radar", "tenant_id": "acme",
        }, headers={"X-API-Key": "sk-beta"})
        assert r.status_code == 200
        assert all(x["id"] != entry_id for x in r.json()["results"])

    def test_get_entry_cross_tenant_404(self):
        client = self._client()
        entry_id = self._save(
            client,
            "Beta specific knowledge: the marine division uses a dedicated mesh network.",
            "marine", "network", "beta", "sk-beta",
        )
        # own tenant: visible
        r = client.get(f"/api/v1/entries/{entry_id}",
                       headers={"X-API-Key": "sk-beta"})
        assert r.status_code == 200
        # other tenant: 404, no existence leak
        r = client.get(f"/api/v1/entries/{entry_id}",
                       headers={"X-API-Key": "sk-acme"})
        assert r.status_code == 404

    def test_list_other_tenant_404(self):
        client = self._client()
        r = client.get("/api/v1/tenants/acme/entries",
                       headers={"X-API-Key": "sk-acme"})
        assert r.status_code == 200
        r = client.get("/api/v1/tenants/beta/entries",
                       headers={"X-API-Key": "sk-acme"})
        assert r.status_code == 404

    def test_wings_scoped_to_key_tenant(self):
        client = self._client()
        self._save(
            client,
            "Acme wing entry: the propulsion team owns the electric motor design.",
            "propulsion", "design", "acme", "sk-acme",
        )
        r = client.get("/api/v1/wings", headers={"X-API-Key": "sk-acme"})
        assert "propulsion" in [w["name"] for w in r.json()]
        r = client.get("/api/v1/wings", headers={"X-API-Key": "sk-beta"})
        assert "propulsion" not in [w["name"] for w in r.json()]

    def test_audit_scoped_to_key_tenant(self):
        client = self._client()
        entry_id = self._save(
            client,
            "Acme confidential: the merger negotiations with Nordia are at term sheet stage.",
            "legal", "mna", "acme", "sk-acme",
            sensitivity="confidential",
        )
        # admin views it -> audit record with tenant acme
        r = client.get(f"/api/v1/entries/{entry_id}",
                       headers={"X-API-Key": "sk-admin"})
        assert r.status_code == 200

        r = client.get("/api/v1/audit/recent",
                       headers={"X-API-Key": "sk-acme"})
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert entries, "expected audit entries for tenant acme"
        assert all(e.get("tenant_id", "default") == "acme" for e in entries)

    def test_key_role_controls_clearance(self):
        client = self._client()
        entry_id = self._save(
            client,
            "Acme HR matter: the bonus pool allocation for the leadership team is decided.",
            "hr", "compensation", "acme", "sk-admin",
            sensitivity="hr_privileged",
        )

        # admin key (clearance: legal) sees it
        r = client.post("/api/v1/search", json={
            "query": "bonus pool", "tenant_id": "acme",
        }, headers={"X-API-Key": "sk-admin"})
        assert any(x["id"] == entry_id for x in r.json()["results"])

        # hr_admin key sees it (role-based clearance)
        r = client.post("/api/v1/search", json={
            "query": "bonus pool", "tenant_id": "acme",
        }, headers={"X-API-Key": "sk-hr"})
        assert any(x["id"] == entry_id for x in r.json()["results"])

        # readwrite acme key (clearance: internal) does not
        r = client.post("/api/v1/search", json={
            "query": "bonus pool", "tenant_id": "acme",
        }, headers={"X-API-Key": "sk-acme"})
        assert all(x["id"] != entry_id for x in r.json()["results"])

        # spoofing an admin role in the body does NOT help
        r = client.post("/api/v1/search", json={
            "query": "bonus pool", "tenant_id": "acme",
            "requester_role": "admin",
        }, headers={"X-API-Key": "sk-acme"})
        assert all(x["id"] != entry_id for x in r.json()["results"])


# ── Cleanup assertion ───────────────────────────────────────

def test_auth_disabled_after_tests():
    """Ensure auth is disabled after all tests (default state)."""
    auth_reset()
    is_enabled = getattr(auth_mod, "AUTH_ENABLED", True)
    assert not is_enabled
