# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Peer keys: issuance, listing, revocation and a peer check.

A key issued to another organisation is a normal ThreadWeave key carrying the
peer fields that auth.KeyInfo reads: who the peer is, a contact, who issued it,
when it expires, which wing it may reach and how many results one query may
return. This module owns the file operations and the CLI is a thin wrapper.

Rule, from docs/ai-publication-boundary.md: a peer read is a read, not a
publication. Nothing here grants a write, and nothing here grants a second wing.
"""
from __future__ import annotations

import json as _json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from threadweave.auth import load_keys_file, write_keys_file

DEFAULT_EXPIRY_DAYS = 90
DEFAULT_MAX_RESULTS = 25
KEY_PREFIX = "sk-peer-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_peer_key(
    peer_id: str,
    wing: str,
    *,
    tenant_id: str = "default",
    contact: str = "",
    issued_by: str = "",
    days: int = DEFAULT_EXPIRY_DAYS,
    max_results: int = DEFAULT_MAX_RESULTS,
    path: Optional[str] = None,
) -> tuple[str, dict]:
    """Create a peer key. The raw key is returned once and never again."""
    if not peer_id or not wing:
        raise ValueError("a peer key needs both a peer_id and a wing")
    data = load_keys_file(path)
    for entry in data.get("keys", []):
        if entry.get("peer_id") == peer_id and not entry.get("revoked"):
            raise ValueError(f"an active key already exists for peer {peer_id!r}")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    record = {
        "key": raw,
        "tenant_id": tenant_id,
        "role": "readonly",
        "label": f"peer:{peer_id}",
        "wing": wing,
        "peer_id": peer_id,
        "peer_contact": contact,
        "issued_by": issued_by,
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(days=int(days))).isoformat(),
        "max_results": int(max_results),
    }
    data.setdefault("keys", []).append(record)
    write_keys_file(data, path)
    return raw, record


def list_peers(path: Optional[str] = None) -> list[dict]:
    """Every peer key on this host, with the key value removed."""
    out = []
    for entry in load_keys_file(path).get("keys", []):
        if not entry.get("peer_id"):
            continue
        record = {k: v for k, v in entry.items() if k != "key"}
        record["revoked"] = bool(entry.get("revoked"))
        out.append(record)
    return out


def revoke_peer_key(peer_id: str, path: Optional[str] = None) -> int:
    """Mark every active key for this peer revoked. Returns how many."""
    data = load_keys_file(path)
    count = 0
    for entry in data.get("keys", []):
        if entry.get("peer_id") == peer_id and not entry.get("revoked"):
            entry["revoked"] = True
            entry["revoked_at"] = _now().isoformat()
            count += 1
    if count:
        write_keys_file(data, path)
    return count


def check_peer(
    url: str,
    key: str,
    query: str,
    *,
    limit: int = DEFAULT_MAX_RESULTS,
    timeout: float = 15.0,
) -> dict:
    """Ask a peer instance one question and report what came back."""
    body = _json.dumps({"query": query, "limit": int(limit)}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/v1/search",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key or ""},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # noqa: BLE001 - reported, not raised, to the operator
        return {"ok": False, "error": str(exc)[:200]}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    return {"ok": True, "status": 200, "count": len(results), "results": results}
