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
    X-API-Key: <key>
    Authorization: Bearer <key>

Tenant isolation:
    Each key maps to a tenant_id. Protected endpoints use
    request.state.tenant_id to scope operations. An admin key
    (tenant_id="*") bypasses tenant scoping.

Requester identity (for confidentiality ACLs):
    keys.json entries may carry "wing" and "person_id" so the API key
    is the trusted source of requester identity when auth is enabled.
    Body/query identity claims are only honored when auth is off
    (development mode).

Example keys.json:
    {
      "keys": [
        {"key": "sk-alice", "tenant_id": "acme", "role": "readwrite",
         "wing": "engineering", "person_id": "alice"},
        {"key": "sk-admin", "tenant_id": "*", "role": "admin"}
      ]
    }

Endpoints exempt from auth (always open):
    GET /api/v1/health
    GET /api/v1/metrics
    GET /api/v1/metrics/prometheus
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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

# -- Peer keys --
# A key issued to another organisation may only search and read single
# entries. Every other route returns more than one question's worth, so a
# peer must not reach it. See docs/ai-publication-boundary.md (R8) and
# docs/peer-keys.md.
PEER_ALLOWED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/search"),
    ("GET", "/api/v1/entries/{entry_id}"),
)


def peer_route_allowed(method: str, path: str) -> bool:
    """True only for the routes a peer key may use."""
    method = (method or "").upper()
    path = (path or "").rstrip("/")
    if method == "POST" and path == "/api/v1/search":
        return True
    if method == "GET" and path.startswith("/api/v1/entries/"):
        entry_id = path[len("/api/v1/entries/"):]
        return bool(entry_id) and "/" not in entry_id
    return False


# Peer keys are rate limited per partner, refilled every second, because a key
# handed to another organisation must not be usable as a bulk export whatever
# its result cap says. 0 disables the limit.
PEER_RATE_LIMIT_ENV = "THREADWEAVE_PEER_RATE_LIMIT"
PEER_RATE_LIMIT_DEFAULT = 60          # requests per minute, per peer
_peer_buckets: dict[str, tuple[float, float]] = {}   # identity -> (tokens, last refill)


def peer_rate_limit() -> int:
    """Requests per minute allowed for one peer, 0 when unlimited."""
    raw = os.environ.get(PEER_RATE_LIMIT_ENV, "").strip()
    if not raw:
        return PEER_RATE_LIMIT_DEFAULT
    return _as_int(raw, PEER_RATE_LIMIT_DEFAULT)


def peer_rate_allow(identity: str, limit: int, now: Optional[float] = None) -> bool:
    """Take one token for this peer. False when the bucket is empty.

    A token bucket rather than a fixed window: a partner asking a steady
    trickle is never pushed to its next window by a single burst, and the
    limit is still a hard ceiling over time.
    """
    if limit <= 0:
        return True
    import time as _time

    moment = _time.monotonic() if now is None else float(now)
    tokens, last = _peer_buckets.get(identity, (float(limit), moment))
    refill = max(0.0, moment - last) * (limit / 60.0)
    tokens = min(float(limit), tokens + refill)
    if tokens < 1.0:
        _peer_buckets[identity] = (tokens, moment)
        return False
    _peer_buckets[identity] = (tokens - 1.0, moment)
    return True


def reset_peer_rate_limits() -> None:
    """Forget every bucket. Used by tests and the CLI."""
    _peer_buckets.clear()


def peer_allowlist() -> list[str]:
    """The allowlist as text, for docs, tests and the CLI."""
    return [f"{m} {p}" for m, p in PEER_ALLOWED_ROUTES]


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# -- Key store --

@dataclass
class KeyInfo:
    tenant_id: str
    role: str
    label: str = ""
    wing: str = ""
    person_id: str = ""
    # Peer keys: set only on a key issued to another organisation
    peer_id: str = ""
    peer_contact: str = ""
    issued_by: str = ""
    issued_at: str = ""
    expires_at: str = ""
    max_results: int = 0

    @property
    def is_peer(self) -> bool:
        """True when this key was issued to another organisation."""
        return bool(self.peer_id)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Fail closed: an expiry that cannot be parsed counts as expired."""
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(str(self.expires_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) >= expires

class KeyStore:
    # The (path, mtime, size) of keys.json as last read. Class-level so the
    # first load, which happens in __init__, can compare against it.
    _last_file_state: tuple = ()

    def __init__(self):
        self._keys: dict[str, KeyInfo] = {}
        self._load_from_env()
        self._load_from_file()

    def _file_state(self) -> tuple:
        """(path, mtime, size) of keys.json, with size -1 when absent."""
        path = Path.home() / ".threadweave" / "keys.json"
        try:
            stat = path.stat()
        except OSError:
            return (str(path), 0.0, -1)
        return (str(path), stat.st_mtime, stat.st_size)

    def reload_if_changed(self) -> bool:
        """Reload keys.json when it changed on disk. True when it did.

        Without this a revocation only reaches a running server on restart, so
        "revocation ends access on the next request" would be false. The check
        is one stat() call per validated request and a read only on change.
        """
        state = self._file_state()
        if state == self._last_file_state:
            return False
        self._last_file_state = state
        self._keys.clear()
        self._load_from_env()
        self._load_from_file()
        return True

    def validate(self, key: str) -> Optional[KeyInfo]:
        self.reload_if_changed()
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
        """Load keys.json, remembering the file state for change detection."""
        self._last_file_state = self._file_state()
        path = Path.home() / ".threadweave" / "keys.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for entry in data.get("keys", []):
            if entry.get("revoked"):
                continue
            key = entry.get("key")
            tenant = entry.get("tenant_id", "default")
            role = entry.get("role", "readwrite")
            label = entry.get("label", f"file:{tenant}")
            if key:
                self._keys[key] = KeyInfo(
                    tenant_id=tenant, role=role, label=label,
                    wing=entry.get("wing", ""),
                    person_id=entry.get("person_id", ""),
                    peer_id=entry.get("peer_id", ""),
                    peer_contact=entry.get("peer_contact", ""),
                    issued_by=entry.get("issued_by", ""),
                    issued_at=entry.get("issued_at", ""),
                    expires_at=entry.get("expires_at", ""),
                    max_results=_as_int(entry.get("max_results", 0)),
                )

# -- Middleware --

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        if not AUTH_ENABLED:
            return await call_next(request)

        # The static shell and the API docs carry no content, and every byte of
        # data comes from /api/... behind the key. Without this the dashboard is
        # unusable in a browser with auth on, because a browser cannot attach an
        # X-API-Key header to the page request itself.
        if not request.url.path.startswith("/api/"):
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
        if info.is_peer:
            if info.is_expired():
                return JSONResponse(
                    {"error": "Peer key expired", "peer": info.peer_id},
                    status_code=403,
                )
            if not peer_route_allowed(request.method, request.url.path):
                return JSONResponse(
                    {
                        "error": "Peer keys may only search and read single entries",
                        "peer": info.peer_id,
                        "allowed": peer_allowlist(),
                    },
                    status_code=403,
                )
            limit = peer_rate_limit()
            if not peer_rate_allow(f"peer:{info.peer_id}", limit):
                return JSONResponse(
                    {
                        "error": "Peer key rate limit exceeded",
                        "peer": info.peer_id,
                        "limit_per_minute": limit,
                    },
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        request.state.is_peer = info.is_peer
        request.state.peer_id = info.peer_id
        request.state.peer_max_results = info.max_results
        request.state.tenant_id = info.tenant_id
        request.state.auth_role = info.role
        request.state.auth_label = info.label
        request.state.auth_wing = info.wing
        request.state.auth_person = info.person_id
        return await call_next(request)

# -- Module instances --

_keystore = KeyStore()

def api_headers() -> dict[str, str]:
    """Headers to send with ThreadWeave API requests.

    Reads THREADWEAVE_API_KEY from the environment (set in a connector's
    daemon env file) and returns an X-API-Key header when present. Returns
    an empty dict when unset so callers stay backwards-compatible with an
    auth-disabled server.
    """
    key = os.environ.get("THREADWEAVE_API_KEY", "").strip()
    if not key:
        return {}
    return {"X-API-Key": key}

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

def validate_key(key: str) -> Optional[KeyInfo]:
    """Validate a raw API key against the loaded key store.

    Public seam for surfaces that authenticate the same keys as the REST
    API (the MCP server), so a key can never be valid on one surface and
    not the other.
    """
    if not key:
        return None
    return _keystore.validate(key)

def require_role(request: Request, role: str) -> bool:
    if not AUTH_ENABLED:
        return True
    actual = getattr(request.state, "auth_role", "readwrite")
    hierarchy = {"admin": 3, "readwrite": 2, "readonly": 1}
    return hierarchy.get(actual, 0) >= hierarchy.get(role, 0)

def is_peer_request(request: Request) -> bool:
    """True when the request carries a key issued to another organisation."""
    return bool(getattr(request.state, "is_peer", False))


def get_peer_id(request: Request) -> str:
    """The peer behind this request, or an empty string."""
    return getattr(request.state, "peer_id", "") or ""


def get_auth_wing(request: Request) -> str:
    """The wing this key is scoped to, or an empty string."""
    return getattr(request.state, "auth_wing", "") or ""


def get_peer_max_results(request: Request) -> int:
    """Result cap for a peer key, 0 when unset or not a peer."""
    return _as_int(getattr(request.state, "peer_max_results", 0))


def revoke_key(key: str) -> bool:
    """Drop a key from the in-memory store. The file is the CLI's job."""
    if key in _keystore._keys:
        del _keystore._keys[key]
        return True
    return False


def load_keys_file(path: Optional[Path] = None) -> dict:
    """Read ~/.threadweave/keys.json, or an empty document."""
    p = Path(path) if path else Path.home() / ".threadweave" / "keys.json"
    if not p.is_file():
        return {"keys": []}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"keys": []}
    if not isinstance(data, dict):
        return {"keys": []}
    data.setdefault("keys", [])
    return data


def write_keys_file(data: dict, path: Optional[Path] = None) -> Path:
    """Write keys.json with owner-only permissions and no BOM."""
    p = Path(path) if path else Path.home() / ".threadweave" / "keys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return p
