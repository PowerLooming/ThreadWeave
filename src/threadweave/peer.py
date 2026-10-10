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
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    record_bridge(
        peer_id,
        wing,
        contact=contact,
        issued_by=issued_by,
        issued_at=record["issued_at"],
        expires_at=record["expires_at"],
    )
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
        revoke_bridge(peer_id, path=None)
    return count


def check_peer(
    url: str,
    key: str,
    query: str,
    *,
    limit: int = DEFAULT_MAX_RESULTS,
    timeout: float = 15.0,
    ca: str = "",
    cert: str = "",
    cert_key: str = "",
) -> dict:
    """Ask a peer instance one question and report what came back.

    ``ca``, ``cert`` and ``cert_key`` present a client certificate, which is
    what a P2 link requires: the server refuses a client that has no
    certificate before it ever reads the key.
    """
    body = _json.dumps({"query": query, "limit": int(limit)}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/v1/search",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key or ""},
        method="POST",
    )
    context = None
    if ca:
        from threadweave.peer_tls import client_ssl_context

        context = client_ssl_context(ca, cert or None, cert_key or None)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # noqa: BLE001 - reported, not raised, to the operator
        return {"ok": False, "error": str(exc)[:200]}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    return {"ok": True, "status": 200, "count": len(results), "results": results}


def peer_activity(peer_id: str) -> dict:
    """What the audit log says this peer did: counts, wings and last read.

    Read straight from the audit trail, so the owner's report is the same
    record the boundary document points at rather than a second counter that
    can drift from it. Repeated identical events are folded into one row with a
    count, so the counts are summed rather than counted.
    """
    from threadweave.confidentiality import get_audit_log

    rows = get_audit_log().get_for_requester(f"peer:{peer_id}")

    def total(items: list[dict]) -> int:
        return sum(int(row.get("count") or 1) for row in items)

    reads = [r for r in rows if r.get("action") == "peer_read"]
    searches = [r for r in rows if r.get("action") == "peer_search"]
    denied = [r for r in rows if r.get("action") == "denied"]
    return {
        "reads": total(reads),
        "searches": total(searches),
        "denied": total(denied),
        "last_read": max((r.get("timestamp", "") for r in rows), default=""),
        "wings_read": sorted({r.get("entry_wing", "") for r in reads if r.get("entry_wing")}),
        "entries_read": sorted({r.get("entry_id", "") for r in reads if r.get("entry_id")})[:50],
    }


def list_peers_with_activity(path: Optional[str] = None) -> list[dict]:
    """Every peer key on this host, with what the audit log says it read."""
    out = []
    for record in list_peers(path):
        if record.get("peer_id"):
            record["activity"] = peer_activity(record["peer_id"])
        out.append(record)
    return out


def bridges(path: Optional[str] = None, include_closed: bool = False) -> list[dict]:
    """The bridge edges: which partner may reach which wing.

    Borrows the palace vocabulary. A room is reachable by halls and tunnels
    inside this palace; a bridge is the one edge that says a second palace may
    reach this room, and it names who is on the other side. Decision 7 keeps it
    in ThreadWeave's own metadata next to the keys, so MemPalace needs no
    change: the bridge is live only while the key exists and is not revoked.

    ``include_closed`` adds the bridges that were closed, from the registry in
    ``bridges.json``, because an owner asking "who could read this wing" wants
    the answer after a partnership ends as well as during it. Keys issued
    before the registry existed are backfilled the first time either view is
    read.
    """
    out = []
    live = {
        record["peer_id"]: record
        for record in list_peers(path)
        if record.get("peer_id") and not record.get("revoked")
    }
    for peer_id, record in live.items():
        out.append({
            "id": f"peer:{peer_id}",
            "peer_id": peer_id,
            "wing": record.get("wing", ""),
            "label": record.get("peer_contact") or peer_id,
            "expires_at": record.get("expires_at", ""),
            "max_results": record.get("max_results", 0),
            "bridge": True,
        })
    if not include_closed:
        return out

    sync_bridges_from_keys(path=path)
    for bridge in load_bridges(path).get("bridges", []):
        peer_id = bridge.get("peer_id", "")
        if not peer_id or peer_id in live:
            continue
        out.append({
            "id": f"peer:{peer_id}",
            "peer_id": peer_id,
            "wing": bridge.get("wing", ""),
            "label": bridge.get("contact") or peer_id,
            "expires_at": bridge.get("expires_at", ""),
            "max_results": 0,
            "bridge": True,
            "active": False,
            "closed_at": bridge.get("revoked_at", ""),
            "issued_by": bridge.get("issued_by", ""),
        })
    return out


# ── Bridges ──────────────────────────────────────────────────
# A bridge is a second palace reaching one room of this one, and it is kept in
# ThreadWeave's own metadata next to the entry store rather than upstream in
# MemPalace. Halls and tunnels say two things here are related; a bridge says
# someone outside may look at this room, and who.

BRIDGES_ENV = "THREADWEAVE_BRIDGES_FILE"


def bridges_file(path: Optional[str] = None) -> Path:
    """Where the bridge registry lives."""
    chosen = path or os.environ.get(BRIDGES_ENV)
    if chosen:
        return Path(chosen)
    return Path.home() / ".threadweave" / "bridges.json"


def load_bridges(path: Optional[str] = None) -> dict:
    p = bridges_file(path)
    if not p.is_file():
        return {"bridges": []}
    try:
        data = _json.loads(p.read_text())
    except (_json.JSONDecodeError, OSError):
        return {"bridges": []}
    if not isinstance(data, dict):
        return {"bridges": []}
    data.setdefault("bridges", [])
    return data


def save_bridges(data: dict, path: Optional[str] = None) -> Path:
    p = bridges_file(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return p


def record_bridge(
    peer_id: str,
    wing: str,
    *,
    contact: str = "",
    issued_by: str = "",
    issued_at: str = "",
    expires_at: str = "",
    path: Optional[str] = None,
) -> dict:
    """Register a bridge.

    A bridge outlives its key on purpose: a revocation is history the owner
    still wants to see, which rooms were reachable and what crossed them.
    """
    data = load_bridges(path)
    record = {
        "peer_id": peer_id,
        "wing": wing,
        "contact": contact,
        "issued_by": issued_by,
        "issued_at": issued_at or _now().isoformat(),
        "expires_at": expires_at,
        "revoked_at": "",
    }
    for existing in data["bridges"]:
        if existing.get("peer_id") == peer_id and not existing.get("revoked_at"):
            existing.update({k: v for k, v in record.items() if v})
            save_bridges(data, path)
            return existing
    data["bridges"].append(record)
    save_bridges(data, path)
    return record


def revoke_bridge(peer_id: str, path: Optional[str] = None) -> int:
    """Close a bridge, keeping the record of it."""
    data = load_bridges(path)
    count = 0
    for entry in data["bridges"]:
        if entry.get("peer_id") == peer_id and not entry.get("revoked_at"):
            entry["revoked_at"] = _now().isoformat()
            count += 1
    if count:
        save_bridges(data, path)
    return count


def sync_bridges_from_keys(keys_file: Optional[str] = None, path: Optional[str] = None) -> int:
    """Backfill bridges for keys issued before the registry existed."""
    data = load_bridges(path)
    known = {b.get("peer_id") for b in data["bridges"]}
    added = 0
    for entry in load_keys_file(keys_file).get("keys", []):
        peer_id = entry.get("peer_id")
        if not peer_id or peer_id in known:
            continue
        data["bridges"].append({
            "peer_id": peer_id,
            "wing": entry.get("wing", ""),
            "contact": entry.get("peer_contact", ""),
            "issued_by": entry.get("issued_by", ""),
            "issued_at": entry.get("issued_at", ""),
            "expires_at": entry.get("expires_at", ""),
            "revoked_at": entry.get("revoked_at", "") if entry.get("revoked") else "",
        })
        added += 1
    if added:
        save_bridges(data, path)
    return added
