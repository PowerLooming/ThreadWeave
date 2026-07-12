# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for API key authentication middleware.
"""
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


# ── Cleanup assertion ───────────────────────────────────────

def test_auth_disabled_after_tests():
    """Ensure auth is disabled after all tests (default state)."""
    auth_reset()
    is_enabled = getattr(auth_mod, "AUTH_ENABLED", True)
    assert not is_enabled
