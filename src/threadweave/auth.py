# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
API key authentication for ThreadWeave.

Opt-in by design: auth is OFF by default. Set THREADWEAVE_REQUIRE_AUTH=1
to enforce API keys on all protected endpoints.

Key loading (priority order):
1. THREADWEAVE_API_KEYS="tenant:key,tenant:key,..."  (env var)
2. ~/.threadweave/keys.json                          (config file)

Key format:
    X-API-Key: sk-...
    Authorization: Bearer sk-...

Tenant isolation:
    Each key maps to a tenant_id. Protected endpoints use
    request.state.tenant_id to scope operations. An admin key
    (tenant_id="*") bypasses tenant scoping.

Endpoints exempt from auth (always open):
    GET /api/v1/health
    GET /api/v1/metrics
    GET /api/v1/metrics/prometheus
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# -- Configuration --

AUTH_ENABLED = os.environ.get("THREADWEAVE_REQUIRE_AUTH", "").lower() in (
    "1", "true", "yes", "on",
)

OPEN_PATHS = frozenset({
    "/api/v1/health",
    "/api/v1/metrics",
    "/api/v1/metrics/prometheus",
})

# -- Key store --

@dataclass
class KeyInfo:
    tenant_id: str
    role: str
    label: str = ""

class KeyStore:
    def __init__(self):
        self._keys: dict[str, KeyInfo] = {}
        self._load_from_env()
        self._load_from_file()

    def validate(self, key: str) -> Optional[KeyInfo]:
        return self._keys.get(key)

    def add(self, key: str, tenant_id: str, role: str = "readwrite", label: str = ""):
        self._keys[key] = KeyInfo(tenant_id=tenant_id, role=role, label=label)

    @property
    def count(self) -> int:
        return len(self._keys)

    def _load_from_env(self) -> None:
        raw = os.environ.get("THREADWEAVE_API_KEYS", "").strip()
        if not raw:
            return
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                tenant, key = entry.split(":", 1)
                tenant = tenant.strip()
                key = key.strip()
                if tenant and key:
                    self._keys[key] = KeyInfo(
                        tenant_id=tenant, role="readwrite", label=f"env:{tenant}",
                    )
            except ValueError:
                pass

    def _load_from_file(self) -> None:
        path = Path.home() / ".threadweave" / "keys.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for entry in data.get("keys", []):
            key = entry.get("key")
            tenant = entry.get("tenant_id", "default")
            role = entry.get("role", "readwrite")
            label = entry.get("label", f"file:{tenant}")
            if key:
                self._keys[key] = KeyInfo(tenant_id=tenant, role=role, label=label)

# -- Middleware --

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        if not AUTH_ENABLED:
            return await call_next(request)

        api_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "")
               .removeprefix("Bearer ")
               .removeprefix("bearer ")
               .strip()
        )
        if not api_key:
            return JSONResponse(
                {"error": "Missing API key"},
                status_code=401,
            )
        info = _keystore.validate(api_key)
        if info is None:
            return JSONResponse(
                {"error": "Invalid API key"},
                status_code=403,
            )
        request.state.tenant_id = info.tenant_id
        request.state.auth_role = info.role
        request.state.auth_label = info.label
        return await call_next(request)

# -- Module instances --

_keystore = KeyStore()

def configure(enabled: bool, keys_env: str = "") -> None:
    global AUTH_ENABLED, _keystore
    AUTH_ENABLED = enabled
    _keystore = KeyStore()
    if keys_env:
        old = os.environ.get("THREADWEAVE_API_KEYS")
        os.environ["THREADWEAVE_API_KEYS"] = keys_env
        try:
            _keystore._load_from_env()
        finally:
            if old is None:
                os.environ.pop("THREADWEAVE_API_KEYS", None)
            else:
                os.environ["THREADWEAVE_API_KEYS"] = old

def reset() -> None:
    global AUTH_ENABLED, _keystore
    AUTH_ENABLED = os.environ.get("THREADWEAVE_REQUIRE_AUTH", "").lower() in (
        "1", "true", "yes", "on",
    )
    _keystore = KeyStore()

# -- Helpers --

def get_tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", "default")
    return "default" if tid == "*" else tid

def require_role(request: Request, role: str) -> bool:
    if not AUTH_ENABLED:
        return True
    actual = getattr(request.state, "auth_role", "readwrite")
    hierarchy = {"admin": 3, "readwrite": 2, "readonly": 1}
    return hierarchy.get(actual, 0) >= hierarchy.get(role, 0)
