# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
ThreadWeave API — FastAPI server for organizational memory.

Central ingestion pipeline: connectors -> ingest -> detect -> store.
Multi-tenant aware. Content deduplication. PII filtering.
"""

import hashlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from threadweave.detector import (
    detect, is_worth_saving, detect_async, is_worth_saving_async,
    ContentType, DetectionResult,
)
from threadweave.llm_detector import get_llm_detector
from threadweave.mempalace_client import MemPalaceClient
from threadweave.org_model import OrgModel
from threadweave.relevance import RelevanceEngine
from threadweave.profiling import metrics, track_latency
from threadweave.auth import APIKeyMiddleware, get_tenant_id
from threadweave.confidentiality import (
    detect_sensitivity,
    RequesterContext,
    SensitivityLevel,
    get_audit_log,
)
from threadweave.store import get_entry_store
from threadweave.notify import get_notification_store

logger = logging.getLogger("threadweave.api")

app = FastAPI(
    title="ThreadWeave API",
    description="Enterprise organizational memory system with central ingestion pipeline",
    version="0.4.0",
)

# Auth middleware (no-op unless THREADWEAVE_REQUIRE_AUTH=true)
app.add_middleware(APIKeyMiddleware)

# CORS — restrict origins with THREADWEAVE_CORS_ORIGINS="https://a,https://b"
# when the API is exposed beyond local development (default: open, matching
# the opt-in auth model).
_cors_origins = [
    o.strip() for o in os.environ.get("THREADWEAVE_CORS_ORIGINS", "*").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Stores ----

_memory_store: dict[str, dict] = {}
_dedup_hashes: set[str] = set()  # Content hashes for deduplication
_org_model = OrgModel()
_mempalace_available = False
_mempalace = MemPalaceClient()   # MemPalace hybrid search + storage client

# Tenant-to-palace mapping (multi-tenancy)
# In production, this maps to per-tenant MemPalace paths
_tenant_stores: dict[str, dict] = {}


# ---- Read-path scoping helpers ----

def _scoped_tenant(request: Request) -> Optional[str]:
    """Tenant to scope a read to, or None for unscoped access.

    Unscoped when auth is off (development) or the key is an admin key
    (tenant_id="*"). Otherwise returns the key's tenant, so a tenant key
    can never read another tenant's data regardless of body/path claims.
    """
    tid = getattr(request.state, "tenant_id", None)
    if tid is None or tid == "*":
        return None
    return tid


def _clearance_for_role(role: str) -> SensitivityLevel:
    """Map an API key role to the confidentiality clearance it grants."""
    return {
        "admin": SensitivityLevel.LEGAL_PRIVILEGED,
        "legal": SensitivityLevel.LEGAL_PRIVILEGED,
        "hr_admin": SensitivityLevel.HR_PRIVILEGED,
    }.get(role, SensitivityLevel.INTERNAL)


def _request_ip_hash(request: Request) -> str:
    """Short sha256 of the client IP for the audit trail (no raw IPs)."""
    host = getattr(request.client, "host", "")
    if not host:
        return ""
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def _requester_from_request(
    request: Request,
    wing: str = "",
    person_id: str = "",
    role: str = "readwrite",
) -> RequesterContext:
    """Build the requester context from TRUSTED key claims when present.

    ``request.state.auth_role`` is only set by the API key middleware for
    requests authenticated with a valid key. When it is set, identity
    comes from the key and unauthenticated body/query claims are ignored.
    When it is absent (auth disabled), body/query claims are honored for
    development use.
    """
    key_role = getattr(request.state, "auth_role", None)
    if key_role is not None:
        return RequesterContext(
            person_id=getattr(request.state, "auth_person", ""),
            wing=getattr(request.state, "auth_wing", ""),
            role=key_role,
            clearance=_clearance_for_role(key_role),
        )
    return RequesterContext(
        person_id=person_id,
        wing=wing,
        role=role,
    )


# ---- Ingest — CENTRAL INGESTION PIPELINE ----


class IngestRequest(BaseModel):
    """Unified ingest from any connector."""
    content: str = Field(..., min_length=1, description="Raw content to process")
    source: str = Field(..., description="teams, sharepoint, email, manual, api")
    tenant_id: str = Field(default="default", description="Tenant/org identifier")
    metadata: dict = Field(default_factory=dict, description="Source-specific metadata")


class IngestResponse(BaseModel):
    """Result from the ingestion pipeline."""
    id: str
    should_save: bool
    content_type: str
    confidence: float
    signals: list[str]
    has_pii: bool
    suggested_title: str
    suggested_scope: str
    deduplicated: bool = False
    detector: str = "regex"  # "llm" or "regex"


# Existing models (kept for backward compatibility)


class DetectRequest(BaseModel):
    text: str = Field(..., min_length=1)
    min_length: int = Field(50)


class DetectResponse(BaseModel):
    should_save: bool
    content_type: str
    confidence: float
    signals: list[str]
    entities: list[dict]
    suggested_scope: str
    suggested_title: str
    has_pii: bool


class SaveRequest(BaseModel):
    content: str = Field(..., min_length=1)
    wing: str = Field(...)
    room: str = Field(default="general")
    scope: str = Field(default="team")
    source_type: str = Field(default="manual")
    author_id: str = Field(default="unknown")
    title: str = Field(default="")
    tenant_id: str = Field(default="default")
    sensitivity: Optional[str] = Field(
        default=None,
        description="Override auto-detected sensitivity: public, internal, "
                    "confidential, restricted, hr_privileged, "
                    "client_confidential, legal_privileged"
    )
    client_id: Optional[str] = Field(
        default=None,
        description="Client/project identifier for client_confidential entries"
    )
    allowed_people: Optional[list[str]] = Field(
        default=None,
        description="List of person IDs allowed to view (for restricted entries)"
    )


class SaveResponse(BaseModel):
    id: str
    wing: str
    room: str
    title: str
    created_at: str


class EntryResponse(BaseModel):
    id: str
    content: str
    wing: str
    room: str
    scope: str
    source_type: str
    author_id: str
    created_at: str
    entities: list[dict]
    version_of: str = ""


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    wing: Optional[str] = None
    room: Optional[str] = None
    requester_team: Optional[str] = None
    requester_role: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
    tenant_id: str = Field(default="default")


class SearchResponse(BaseModel):
    results: list[dict]
    total: int
    query: str


class OrgRelationshipRequest(BaseModel):
    source: str
    relation: str
    target: str
    valid_from: str
    valid_to: Optional[str] = None


class OrgMembershipResponse(BaseModel):
    person_id: str
    team: Optional[str]
    as_of: str


class HealthResponse(BaseModel):
    status: str
    version: str
    mempalace_available: bool
    entries_stored: int
    dedup_cache_size: int
    tenants_active: int
    uptime_seconds: float
    detector: str = "regex"  # "llm" or "regex"


# ---- Startup ----

_start_time = datetime.now(timezone.utc)


# ---- Startup (lifespan) ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: detect MemPalace availability once."""
    global _mempalace_available
    _mempalace_available = _mempalace.available
    if _mempalace_available:
        logger.info(
            "MemPalace hybrid search available at %s", _mempalace.palace_path
        )

    # Durability: reload persisted entries into the memory stores so the
    # palace survives restarts (SQLite at ~/.threadweave/entries.sqlite3).
    try:
        persisted = get_entry_store().load_all()
        for entry in persisted:
            _memory_store[entry["id"]] = entry
            _tenant_stores.setdefault(
                entry.get("tenant_id", "default"), {}
            )[entry["id"]] = entry
        if persisted:
            logger.info(
                "Restored %d entries from %s", len(persisted),
                get_entry_store().path,
            )
    except Exception as exc:
        logger.warning("Entry store reload failed: %s", exc)
    yield


# FastAPI accepts lifespan only at construction; the router attribute is
# the same hook and can be set after the app object exists (the stores it
# references are defined above).
app.router.lifespan_context = lifespan


# ---- Health ----

@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="healthy",
        version="0.4.0",
        mempalace_available=_mempalace_available,
        entries_stored=len(_memory_store),
        dedup_cache_size=len(_dedup_hashes),
        tenants_active=len(_tenant_stores),
        uptime_seconds=(datetime.now(timezone.utc) - _start_time).total_seconds(),
        detector="llm" if get_llm_detector() else "regex",
    )


# ---- Metrics / Profiling ----


@app.get("/api/v1/metrics")
async def get_metrics():
    """Pipeline metrics as JSON — latencies, counters, throughput, memory."""
    return metrics.to_dict()


@app.get("/api/v1/metrics/prometheus")
async def get_metrics_prometheus():
    """Pipeline metrics in Prometheus text format — scrape target."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=metrics.to_prometheus(), media_type="text/plain")


# ---- INGEST — Central Pipeline ----


@app.post("/api/v1/ingest", response_model=IngestResponse, status_code=201)
@track_latency(metrics.ingest_latency)
async def ingest_content(req: IngestRequest, request: Request):
    """
    Central ingestion pipeline — called by all connectors.

    Pipeline: dedup -> detect -> filter PII -> store

    Accepts content from Teams, SharePoint, Email, or any source.
    Handles deduplication, content detection, and storage in one call.

    When auth is enabled, the tenant_id from the API key overrides
    any tenant_id in the request body (enforced scoping).
    """
    # Auth-enforced tenant scoping
    effective_tenant = get_tenant_id(request)
    if effective_tenant != "default":
        req.tenant_id = effective_tenant

    # 0. Opt-out gate — "camera sign" layer: never store knowledge from
    #    a person who declined harvesting. Checked on every identity the
    #    connectors report: author_id, email_sender, participants.
    from threadweave.optout import get_optout_store

    optout = get_optout_store()
    meta = req.metadata
    identities = [
        meta.get("author_id", ""), meta.get("email_sender", ""),
    ] + [p.strip() for p in meta.get("email_participants", "").split(",")]
    identities += list(meta.get("participants", []) or [])
    if optout.any_opted_out(identities):
        metrics.record_ingest(skipped=True)
        return IngestResponse(
            id="opted_out",
            should_save=False,
            content_type="chat",
            confidence=0.0,
            signals=["opted_out"],
            has_pii=False,
            suggested_title="",
            suggested_scope="team",
            detector="regex",
        )
    detector_mode = "regex"  # default; updated after detection
    # 1. Dedup — hash content + key metadata to avoid false dedup
    # when two emails share a body (templates) but have different subjects/senders.
    # Tenant is part of the key: each tenant has its own memory.
    t0 = time.monotonic()
    title = req.metadata.get("title", "")
    author = req.metadata.get("author_id", "")
    dedup_key = f"{req.tenant_id}|{req.content}|{title}|{author}"
    content_hash = hashlib.sha256(dedup_key.encode()).hexdigest()
    if content_hash in _dedup_hashes:
        metrics.dedup_latency.record((time.monotonic() - t0) * 1000)
        metrics.dedup_hits += 1
        metrics.record_ingest(skipped=True)
        return IngestResponse(
            id="duplicate",
            should_save=False,
            content_type="chat",
            confidence=1.0,
            signals=["duplicate"],
            has_pii=False,
            suggested_title="",
            suggested_scope="team",
            deduplicated=True,
            detector=detector_mode,
        )
    # NOTE: the hash is added to _dedup_hashes only when the entry is
    # actually saved (step 5 below). Rejected (PII) or skipped content
    # must stay retryable within the same server run.
    metrics.dedup_latency.record((time.monotonic() - t0) * 1000)

    # 2. Detect — classify content (async, tries LLM first, regex fallback)
    t0 = time.monotonic()
    should_save, result = await is_worth_saving_async(req.content)
    metrics.detect_latency.record((time.monotonic() - t0) * 1000)

    # Track LLM vs regex usage from the detection signals
    signals = result.signals
    if any("llm(" in s for s in signals):
        # LLM was used — check if it was a hit or the LLM itself fell back
        detector_mode = "llm"
        metrics.record_detect(llm_hit=True)
    else:
        # Regex was used (either no key configured or LLM call failed)
        detector_mode = "regex"
        metrics.record_detect(regex_fallback=True)

    # 3. PII gate — reject if PII detected
    if result.has_pii:
        metrics.record_ingest(rejected_pii=True)
        return IngestResponse(
            id="rejected_pii",
            should_save=False,
            content_type=result.content_type.value,
            confidence=result.confidence,
            signals=signals + ["pii_rejected"],
            has_pii=True,
            suggested_title=result.suggested_title,
            suggested_scope=result.suggested_scope,
            detector=detector_mode,
        )

    # 4. Check if worth saving
    if not should_save:
        metrics.record_ingest(skipped=True)
        return IngestResponse(
            id="not_saved",
            should_save=False,
            content_type=result.content_type.value,
            confidence=result.confidence,
            signals=signals,
            has_pii=result.has_pii,
            suggested_title=result.suggested_title,
            suggested_scope=result.suggested_scope,
            detector=detector_mode,
        )

    # 5. Store — per-tenant isolation
    t0 = time.monotonic()
    entry_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    # Sensitivity detection
    sens = detect_sensitivity(req.content)

    entry = {
        "id": entry_id,
        "content": req.content,
        "wing": req.metadata.get("wing", req.source),
        "room": req.metadata.get("room", result.content_type.value),
        "scope": result.suggested_scope,
        "source_type": req.source,
        "author_id": (
            req.metadata.get("author_id")
            or req.metadata.get("email_sender")
            or "unknown"
        ),
        "title": req.metadata.get("title") or result.suggested_title,
        "created_at": now,
        "entities": result.entities,
        "content_type": result.content_type.value,
        "has_pii": result.has_pii,
        "tenant_id": req.tenant_id,
        "source_metadata": req.metadata,
        "sensitivity": sens.suggested_level.value,
        "client_id": req.metadata.get("client_id"),
        "allowed_people": req.metadata.get("allowed_people", []),
    }

    # Mark as seen only after the entry is actually stored
    _dedup_hashes.add(content_hash)

    # Store in tenant-isolated store
    tenant_store = _tenant_stores.setdefault(req.tenant_id, {})
    tenant_store[entry_id] = entry
    _memory_store[entry_id] = entry  # Also global for search

    # Version chaining BEFORE the save: a re-captured document (same
    # source_file) is a new version of the earlier capture, not a
    # standalone duplicate. (Must run before the entry is persisted —
    # otherwise find_by_source_key sees the new entry itself and the
    # chain check `earlier[-1] != entry_id` fails.)
    source_key = req.metadata.get("source_file", "")
    if source_key:
        try:
            earlier = get_entry_store().find_by_source_key(source_key)
            if earlier and earlier[-1]["id"] != entry_id:
                entry["version_of"] = earlier[-1]["id"]
        except Exception as exc:
            logger.warning("Version chaining failed: %s", exc)

    # Durability: write-through to SQLite so the palace survives restarts
    try:
        get_entry_store().save(entry)
    except Exception:
        pass  # persistence is best-effort; memory store is authoritative

    # Capture notification ("camera sign"): queue a DM for the content
    # author so they know their material was saved. Opted-out authors
    # never reach this point (the opt-out gate runs earlier).
    try:
        author = req.metadata.get("author_id") or req.metadata.get(
            "email_sender", ""
        )
        if author:
            notif_id = hashlib.sha256(
                f"{entry_id}:{author}".encode()
            ).hexdigest()[:16]
            get_notification_store().enqueue(
                notification_id=notif_id,
                entry_id=entry_id,
                author_id=author,
                title=entry["title"],
                wing=entry["wing"],
                room=entry["room"],
                source=req.source,
                created_at=entry["created_at"],
            )
    except Exception as exc:
        logger.warning("Notification enqueue failed: %s", exc)

    # 6. MemPalace (if available)
    if _mempalace_available:
        try:
            _mempalace.add_drawer(
                content=req.content,
                wing=entry["wing"],
                room=entry["room"],
                title=entry["title"],
                source=req.source,
                created_at=now,
                author_id=req.metadata.get("author_id", ""),
                content_type=result.content_type.value,
                drawer_id=entry_id,
                tenant_id=req.tenant_id,
                sensitivity=entry["sensitivity"],
            )
        except Exception as exc:
            logger.warning("MemPalace write failed for ingest %s: %s", entry_id, exc)
    metrics.mempalace_write_latency.record((time.monotonic() - t0) * 1000)

    metrics.record_ingest(saved=True)
    return IngestResponse(
        id=entry_id,
        should_save=True,
        content_type=result.content_type.value,
        confidence=result.confidence,
        signals=result.signals,
        has_pii=result.has_pii,
        suggested_title=entry["title"],
        suggested_scope=result.suggested_scope,
        detector=detector_mode,
    )


@app.get("/api/v1/tenants/{tenant_id}/entries")
async def list_tenant_entries(tenant_id: str, request: Request):
    """List all entries for a specific tenant.

    With auth enabled, a tenant key may only list its own tenant;
    anything else returns 404 (no existence leak).
    """
    scoped = _scoped_tenant(request)
    if scoped and scoped != tenant_id:
        raise HTTPException(status_code=404, detail="Tenant not found")
    store = _tenant_stores.get(tenant_id, {})
    return [{"id": eid, "title": e.get("title", ""), "created_at": e.get("created_at", "")}
            for eid, e in store.items()]
# ---- Detection (legacy endpoint, kept for backward compatibility) ----

@app.post("/api/v1/detect", response_model=DetectResponse)
async def detect_content(req: DetectRequest):
    should_save, result = is_worth_saving(req.text)
    return DetectResponse(
        should_save=should_save,
        content_type=result.content_type.value,
        confidence=result.confidence,
        signals=result.signals,
        entities=result.entities,
        suggested_scope=result.suggested_scope,
        suggested_title=result.suggested_title,
        has_pii=result.has_pii,
    )


# ---- Save (also calls ingestion pipeline internally) ----

@app.post("/api/v1/entries", response_model=SaveResponse, status_code=201)
async def save_entry(req: SaveRequest, request: Request):
    # Auth-enforced tenant scoping (mirrors ingest)
    scoped = _scoped_tenant(request)
    if scoped:
        req.tenant_id = scoped

    entry_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    det_result = detect(req.content)

    # Sensitivity detection — user override or auto-detect
    if req.sensitivity:
        try:
            sensitivity = req.sensitivity
            SensitivityLevel(sensitivity)  # Validate
        except ValueError:
            sensitivity = "internal"
    else:
        sens = detect_sensitivity(req.content)
        sensitivity = sens.suggested_level.value

    entry = {
        "id": entry_id,
        "content": req.content,
        "wing": req.wing,
        "room": req.room,
        "scope": req.scope,
        "source_type": req.source_type,
        "author_id": req.author_id,
        "title": req.title or det_result.suggested_title,
        "created_at": now,
        "entities": det_result.entities,
        "content_type": det_result.content_type.value,
        "has_pii": det_result.has_pii,
        "tenant_id": req.tenant_id,
        "sensitivity": sensitivity,
        "client_id": req.client_id,
        "allowed_people": req.allowed_people or [],
    }

    _memory_store[entry_id] = entry
    tenant_store = _tenant_stores.setdefault(req.tenant_id, {})
    tenant_store[entry_id] = entry

    # Durability: write-through to SQLite so the palace survives restarts
    try:
        get_entry_store().save(entry)
    except Exception:
        pass  # persistence is best-effort; memory store is authoritative

    # Also store in MemPalace for semantic search
    if _mempalace_available:
        try:
            _mempalace.add_drawer(
                content=req.content,
                wing=req.wing,
                room=req.room,
                title=entry["title"],
                source=req.source_type,
                created_at=now,
                author_id=req.author_id,
                content_type=det_result.content_type.value,
                drawer_id=entry_id,
                tenant_id=req.tenant_id,
                sensitivity=sensitivity,
            )
        except Exception as exc:
            logger.warning("MemPalace write failed for entry %s: %s", entry_id, exc)

    return SaveResponse(
        id=entry_id, wing=req.wing, room=req.room,
        title=entry["title"], created_at=now,
    )


# ---- Get Entry ----

@app.get("/api/v1/entries/{entry_id}", response_model=EntryResponse)
async def get_entry(
    entry_id: str,
    request: Request,
    person_id: Optional[str] = Query(None),
    wing: Optional[str] = Query(None),
    role: str = Query("readwrite"),
):
    entry = _memory_store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # Tenant scoping: a tenant key must not see other tenants' entries
    scoped = _scoped_tenant(request)
    if scoped and entry.get("tenant_id", "default") != scoped:
        raise HTTPException(status_code=404, detail="Entry not found")

    # Confidentiality enforcement — identity from key claims when auth is
    # on, from query params when auth is off (development)
    requester = _requester_from_request(
        request,
        wing=wing or "",
        person_id=person_id or "",
        role=role,
    )
    if not requester.can_see(entry):
        audit = get_audit_log()
        audit.log_denied(
            requester, entry, "Insufficient clearance for direct access",
            ip_hash=_request_ip_hash(request),
        )
        raise HTTPException(status_code=403, detail="Access denied")

    # Audit: log access to sensitive entries
    audit = get_audit_log()
    audit.log_access(requester, entry, action="view", ip_hash=_request_ip_hash(request))

    return EntryResponse(
        id=entry["id"], content=entry["content"], wing=entry["wing"],
        room=entry["room"], scope=entry["scope"],
        source_type=entry["source_type"], author_id=entry["author_id"],
        created_at=entry["created_at"], entities=entry["entities"],
        version_of=entry.get("version_of", "") or "",
    )


# ---- Delete Entry (the "camera sign" layer: right to delete) ----

@app.delete("/api/v1/entries/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: str,
    request: Request,
    person_id: Optional[str] = Query(None),
    wing: Optional[str] = Query(None),
    role: str = Query("readwrite"),
):
    """Delete an entry. The requester must be the author, in the same
    wing, or have admin/legal/hr clearance. Deletions are audited."""
    entry = _memory_store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    scoped = _scoped_tenant(request)
    if scoped and entry.get("tenant_id", "default") != scoped:
        raise HTTPException(status_code=404, detail="Entry not found")

    requester = _requester_from_request(
        request, wing=wing or "", person_id=person_id or "", role=role,
    )

    # Deletion rights: the entry's author, or a same-wing member for
    # non-sensitive entries, or admin/legal/hr.
    author = entry.get("author_id", "")
    sensitivity = entry.get("sensitivity", "internal")
    can_delete = (
        requester.role in ("admin", "legal", "hr_admin")
        or (author and requester.person_id == author)
        or (
            requester.wing
            and entry.get("wing") == requester.wing
            and sensitivity in ("public", "internal")
        )
    )
    if not can_delete:
        audit = get_audit_log()
        audit.log_denied(
            requester, entry, "Insufficient rights to delete",
            ip_hash=_request_ip_hash(request),
        )
        raise HTTPException(status_code=403, detail="Access denied")

    # Audit the deletion (always — deletions are permanent)
    audit = get_audit_log()
    audit.log_delete(requester, entry, reason="user requested",
                     ip_hash=_request_ip_hash(request))

    # Remove from both stores
    tenant_store = _tenant_stores.get(entry.get("tenant_id", "default"), {})
    tenant_store.pop(entry_id, None)
    _memory_store.pop(entry_id, None)
    # Durability: mirror the deletion
    try:
        get_entry_store().delete(entry_id)
    except Exception:
        pass
    return Response(status_code=204)


# ---- Capture notifications (bot polling) ----

@app.get("/api/v1/notifications/pending")
async def notifications_pending(limit: int = Query(50)):
    """Return undelivered capture notifications (Teams bot polls this)."""
    return {"notifications": get_notification_store().pending(limit=limit)}


@app.post("/api/v1/notifications/{notification_id}/delivered")
async def notification_delivered(notification_id: str, status: str = "delivered"):
    """Mark a notification as delivered (after the bot DMs the author).

    status=skipped marks it undeliverable (no personal conversation ref
    and activity-feed delivery failed after N attempts) so it stops
    retrying without being reported as delivered.
    """
    skipped = status == "skipped"
    get_notification_store().mark_delivered(notification_id, skipped=skipped)
    return {"id": notification_id, "delivered": True,
            "status": "skipped" if skipped else "delivered"}


@app.get("/api/v1/notifications/stats")
async def notifications_stats():
    store = get_notification_store()
    return {"pending": store.count(delivered_only=False),
            "delivered": store.count(delivered_only=True),
            "skipped": store.count_skipped()}


# ---- Entry version chain ----

@app.get("/api/v1/entries/{entry_id}/versions")
async def entry_versions(entry_id: str):
    """Return the version chain for an entry (oldest -> newest)."""
    entry = _memory_store.get(entry_id)
    if not entry:
        entry = get_entry_store().get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    source_key = (entry.get("source_metadata") or {}).get("source_file", "")
    chain = []
    if source_key:
        for e in get_entry_store().find_by_source_key(source_key):
            chain.append({
                "id": e["id"],
                "title": e.get("title", ""),
                "created_at": e.get("created_at", ""),
                "version_of": e.get("version_of"),
                "wing": e.get("wing", ""),
            })
    else:
        # Walk the version_of pointers
        seen = set()
        current: dict | None = entry
        while current and current["id"] not in seen:
            seen.add(current["id"])
            chain.append({
                "id": current["id"],
                "title": current.get("title", ""),
                "created_at": current.get("created_at", ""),
                "version_of": current.get("version_of"),
                "wing": current.get("wing", ""),
            })
            parent_id = current.get("version_of")
            if not parent_id:
                break
            current = _memory_store.get(parent_id) or get_entry_store().get(parent_id)
        chain.reverse()
    return {"entry_id": entry_id, "versions": chain}


# ---- Opt-out registry (the "camera sign" layer) ----

class OptOutRequest(BaseModel):
    person: str = Field(..., min_length=1,
                        description="Email or person ID to opt out/in")


@app.get("/api/v1/optout")
async def list_optouts():
    """List all opted-out identities (privacy admin view)."""
    from threadweave.optout import get_optout_store

    return {"opted_out": get_optout_store().list_opted_out()}


@app.post("/api/v1/optout/out")
async def optout_register(req: OptOutRequest):
    """Register a person as opted out of harvesting."""
    from threadweave.optout import get_optout_store

    added = get_optout_store().opt_out(req.person)
    return {"person": req.person.lower(), "opted_out": True, "changed": added}


@app.post("/api/v1/optout/in")
async def optout_remove(req: OptOutRequest):
    """Remove a person from the opt-out registry."""
    from threadweave.optout import get_optout_store

    removed = get_optout_store().opt_in(req.person)
    return {"person": req.person.lower(), "opted_out": False, "changed": removed}


# ---- Search (MemPalace hybrid + keyword fallback, tenant-aware, confidentiality-filtered) ----

@app.post("/api/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request):
    """Search organizational memory.

    Uses MemPalace hybrid search (BM25 + vector cosine) when available,
    falling back to keyword matching in the in-memory store.

    Wing/room filters narrow results to a specific team/topic.
    Confidentiality: Results are filtered based on requester clearance.
    """
    # Auth-enforced tenant scoping: a tenant key can only search its own
    # tenant, regardless of the tenant_id in the body. Admin keys (and
    # auth-off development) keep the body tenant_id.
    scoped = _scoped_tenant(request)
    if scoped:
        req.tenant_id = scoped

    query_lower = req.query.lower()
    results = []
    seen_ids: set[str] = set()

    # Build requester context — trusted key claims when auth is on,
    # body claims otherwise (see _requester_from_request)
    requester = _requester_from_request(
        request,
        wing=req.requester_team or "",
        person_id=req.requester_team or "",  # requester_team doubles as person_id
        role=req.requester_role or "readwrite",
    )

    # ── 1. MemPalace hybrid search ──
    if _mempalace_available:
        try:
            mp_results = _mempalace.search(
                query=req.query,
                wing=req.wing,
                room=req.room,
                limit=req.limit * 2,  # Fetch extra to account for filtering
            )
            for mr in mp_results:
                if mr.drawer_id in seen_ids:
                    continue
                # Tenant scoping: skip results from other tenants. Entries
                # without a tenant_id predate the field and stay visible.
                if tenants and (mr.tenant_id or "default") not in tenants:
                    continue
                seen_ids.add(mr.drawer_id)
                results.append({
                    "id": mr.drawer_id,
                    "title": "",
                    "wing": mr.wing,
                    "room": mr.room,
                    "content_preview": mr.content[:200],
                    "created_at": mr.created_at,
                    "author_team": mr.wing,
                    "author_id": mr.author_id or "",
                    "version_of": getattr(mr, "version_of", "") or "",
                    "relevance_score": round(mr.similarity, 3),
                    "bm25_score": mr.bm25_score,
                    "source": "mempalace",
                    "sensitivity": mr.sensitivity or "internal",
                })
        except Exception as exc:
            logger.warning(
                "MemPalace search failed, falling back to keyword: %s", exc
            )

    # ── 2. Keyword fallback (in-memory store) ──
    tenants = [req.tenant_id] if req.tenant_id != "default" else None

    for entry_id, entry in _memory_store.items():
        if entry_id in seen_ids:
            continue
        if tenants and entry.get("tenant_id", "default") not in tenants:
            continue
        if req.wing and entry["wing"] != req.wing:
            continue
        if req.room and entry["room"] != req.room:
            continue

        content_lower = entry["content"].lower()
        title_lower = entry.get("title", "").lower()
        score = 0.0
        if query_lower in content_lower:
            score = 0.8
        elif query_lower in title_lower:
            score = 0.6
        elif any(word in content_lower for word in query_lower.split()):
            score = 0.3
        if score > 0:
            seen_ids.add(entry_id)
            results.append({
                "id": entry_id,
                "title": entry.get("title", ""),
                "wing": entry["wing"],
                "room": entry["room"],
                "content_preview": entry["content"][:200],
                "created_at": entry["created_at"],
                "author_team": entry["wing"],
                "author_id": entry.get("author_id", ""),
                "version_of": entry.get("version_of", ""),
                "relevance_score": score,
                "content_type": entry.get("content_type", "unknown"),
                "source": "in_memory",
                "sensitivity": entry.get("sensitivity", "internal"),
            })

    # ── 3. Confidentiality filtering ──
    visible = requester.filter_results(results)
    denied_count = len(results) - len(visible)

    # Audit log: record denied access attempts
    audit = get_audit_log()
    denied_ids = {r["id"] for r in results} - {r["id"] for r in visible}
    ip_hash = _request_ip_hash(request)
    for entry_id in denied_ids:
        entry = _memory_store.get(entry_id, {})
        audit.log_denied(requester, entry, "Insufficient clearance", ip_hash=ip_hash)

    # Sort by relevance
    visible.sort(key=lambda r: r["relevance_score"], reverse=True)
    visible = visible[:req.limit]

    return SearchResponse(
        results=visible, total=len(visible), query=req.query,
    )


# ---- Wings (Teams) ----

@app.get("/api/v1/wings")
async def list_wings(request: Request):
    scoped = _scoped_tenant(request)
    wings = {}
    for entry in _memory_store.values():
        if scoped and entry.get("tenant_id", "default") != scoped:
            continue
        wing = entry["wing"]
        if wing not in wings:
            wings[wing] = 0
        wings[wing] += 1
    return [{"name": name, "entry_count": count}
            for name, count in sorted(wings.items())]


# ---- Rooms (Topics) ----

@app.get("/api/v1/wings/{wing}/rooms")
async def list_rooms(wing: str, request: Request):
    scoped = _scoped_tenant(request)
    rooms = {}
    for entry in _memory_store.values():
        if scoped and entry.get("tenant_id", "default") != scoped:
            continue
        if entry["wing"] == wing:
            room = entry["room"]
            if room not in rooms:
                rooms[room] = 0
            rooms[room] += 1
    return [{"name": name, "entry_count": count}
            for name, count in sorted(rooms.items())]


# ---- Org Model ----

@app.post("/api/v1/org/relationships", status_code=201)
async def add_org_relationship(req: OrgRelationshipRequest):
    _org_model.add_entity(req.source, req.source, "person")
    _org_model.add_entity(req.target, req.target, "team")
    rel = _org_model.add_relationship(
        req.source, req.relation, req.target,
        valid_from=req.valid_from, valid_to=req.valid_to,
    )
    return {"status": "created", "relationship": str(rel)}


@app.get("/api/v1/org/people/{person_id}/team")
async def get_person_team(
    person_id: str,
    as_of: Optional[str] = Query(None),
):
    team = _org_model.get_team(person_id, as_of=as_of)
    return OrgMembershipResponse(
        person_id=person_id,
        team=team,
        as_of=as_of or datetime.now(timezone.utc).date().isoformat(),
    )


class GraphResponse(BaseModel):
    nodes: list[dict]
    edges: list[dict]


@app.get("/api/v1/org/graph", response_model=GraphResponse)
async def get_org_graph(
    wing: Optional[str] = Query(None),
    depth: int = Query(default=2, ge=1, le=5),
):
    """Return the org graph as nodes + edges for visualization.

    Nodes: people, teams, domains with their types and wing membership.
    Edges: member_of, reports_to, owns, collaborates_with, subteam_of.
    Each edge is classified as 'hallway' (within-wing) or 'tunnel' (cross-wing).

    Args:
        wing: Filter to a specific wing/team. If None, returns the full graph.
        depth: How many hops to traverse from the root wing (default 2).
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    # Collect relationships from the org model
    relationships = _org_model.relationships

    # If a wing is specified, build a subgraph centered on that wing
    if wing:
        # Start with the wing itself
        if wing in _org_model.entities:
            entity = _org_model.entities[wing]
            nodes[wing] = {
                "id": wing, "label": entity.name or wing, "type": entity.entity_type,
                "wing": wing,
            }

        # BFS to collect connected nodes up to `depth` hops
        frontier = {wing}
        seen = {wing}
        seen_edges: set[tuple] = set()
        for hop in range(depth):
            next_frontier: set[str] = set()
            for rel in relationships:
                src_in = rel.source in frontier
                tgt_in = rel.target in frontier
                if not (src_in or tgt_in):
                    continue

                # Add both nodes
                for node_id in (rel.source, rel.target):
                    if node_id not in nodes:
                        label = node_id
                        ntype = "unknown"
                        node_wing = ""
                        if node_id in _org_model.entities:
                            e = _org_model.entities[node_id]
                            label = e.name or node_id
                            ntype = e.entity_type
                        # Determine wing:
                        # - Teams ARE wings (their wing is themselves)
                        # - People find their wing via member_of
                        if ntype == "team":
                            node_wing = node_id
                        else:
                            for r2 in relationships:
                                if r2.source == node_id and r2.relation == "member_of":
                                    node_wing = r2.target
                                    break
                        nodes[node_id] = {
                            "id": node_id, "label": label, "type": ntype,
                            "wing": node_wing,
                        }

                edge_key = (rel.source, rel.target, rel.relation)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)

                # Classify edge
                # Determine which wings the source and target belong to
                src_wing = nodes.get(rel.source, {}).get("wing", "")
                tgt_wing = nodes.get(rel.target, {}).get("wing", "")
                is_cross_wing = (
                    src_wing and tgt_wing and src_wing != tgt_wing
                    and rel.relation not in ("member_of", "reports_to", "subteam_of")
                )
                edge_type = "tunnel" if is_cross_wing else "hallway"

                edges.append({
                    "source": rel.source,
                    "target": rel.target,
                    "relation": rel.relation,
                    "type": edge_type,
                })

                if src_in and rel.target not in seen:
                    next_frontier.add(rel.target)
                    seen.add(rel.target)
                if tgt_in and rel.source not in seen:
                    next_frontier.add(rel.source)
                    seen.add(rel.source)

            frontier = next_frontier
            if not frontier:
                break
    else:
        # Full graph — include all entities and relationships
        for entity_id, entity in _org_model.entities.items():
            # Teams are their own wing; people find wing via member_of
            wing_id = entity_id if entity.entity_type == "team" else ""
            if entity.entity_type != "team":
                for rel in relationships:
                    if rel.source == entity_id and rel.relation == "member_of":
                        wing_id = rel.target
                        break
            nodes[entity_id] = {
                "id": entity_id, "label": entity.name or entity_id,
                "type": entity.entity_type, "wing": wing_id,
            }

        for rel in relationships:
            src_wing = nodes.get(rel.source, {}).get("wing", "")
            tgt_wing = nodes.get(rel.target, {}).get("wing", "")
            is_cross_wing = (
                src_wing and tgt_wing and src_wing != tgt_wing
                and rel.relation not in ("member_of", "reports_to", "subteam_of")
            )
            edges.append({
                "source": rel.source,
                "target": rel.target,
                "relation": rel.relation,
                "type": "tunnel" if is_cross_wing else "hallway",
            })

    # Knowledge entry nodes — attach captured knowledge to the org
    # graph so "who knows what" is visible: each entry becomes a node
    # linked to its author (authored_by) and its wing (belongs_to).
    entry_nodes, entry_edges = _entry_graph_nodes(
        wing=wing,
        author_ids={eid: n.get("label", eid)
                    for eid, n in nodes.items()
                    if n.get("type") == "person"},
        known_node_ids=set(nodes.keys()),
    )
    nodes.update(entry_nodes)
    edges.extend(entry_edges)

    return GraphResponse(nodes=list(nodes.values()), edges=edges)


def _entry_graph_nodes(
    wing: Optional[str] = None,
    author_ids: Optional[dict[str, str]] = None,
    known_node_ids: Optional[set[str]] = None,
    max_per_wing: int = 12,
) -> tuple[dict[str, dict], list[dict]]:
    """Build entry nodes + edges from the entry stores.

    An entry node carries id, label (title), wing, room, and source
    so the dashboard can render knowledge attached to the org. Edges:
    authored_by (entry -> person when the author resolves) and
    belongs_to (entry -> wing/team when the wing is a node).
    """
    author_ids = author_ids or {}
    known_node_ids = known_node_ids or set()
    entries = list(get_entry_store().load_all())
    if wing:
        entries = [e for e in entries if e.get("wing") == wing]

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for entry in entries[:max_per_wing]:
        eid = entry["id"]
        author = entry.get("author_id") or ""
        node_wing = entry.get("wing") or ""
        nodes[eid] = {
            "id": eid,
            "label": entry.get("title") or entry.get("content", "")[:40],
            "type": "entry",
            "wing": node_wing,
            "room": entry.get("room", ""),
            "source": entry.get("source_type", ""),
        }
        # belongs_to the wing/team — only when the wing is a real node
        # (known_node_ids includes the filtered wing when it's an org
        # entity, and all teams in the full graph; wings that aren't
        # entities, e.g. the 'email' fallback, get no dangling edge)
        if node_wing and node_wing in known_node_ids:
            edges.append({
                "source": eid,
                "target": node_wing,
                "relation": "belongs_to",
                "type": "hallway",
            })
        # authored_by the person when the author resolves to a node
        if author and author in author_ids:
            edges.append({
                "source": eid,
                "target": author,
                "relation": "authored_by",
                "type": "hallway",
            })
    return nodes, edges

# ---- Audit Log ----


@app.get("/api/v1/audit/recent")
async def get_audit_recent(
    limit: int = Query(default=50, ge=1, le=500),
    request: Request = None,
):
    """Get recent audit log entries for sensitive content access.

    With auth enabled, a tenant key only sees audit entries for its own
    tenant. Admin keys and auth-off development see everything.
    """
    audit = get_audit_log()
    scoped = _scoped_tenant(request)
    entries = audit.get_recent(limit)
    if scoped:
        entries = [e for e in entries if e.get("tenant_id", "default") == scoped]
    return {
        "entries": entries,
        "total": len(entries),
    }


@app.get("/api/v1/audit/entry/{entry_id}")
async def get_audit_for_entry(entry_id: str, request: Request):
    """Get audit log for a specific knowledge entry."""
    audit = get_audit_log()
    scoped = _scoped_tenant(request)
    entries = audit.get_for_entry(entry_id)
    if scoped:
        entries = [e for e in entries if e.get("tenant_id", "default") == scoped]
    return {
        "entry_id": entry_id,
        "entries": entries,
    }


@app.get("/api/v1/audit/requester/{requester_id}")
async def get_audit_for_requester(requester_id: str, request: Request):
    """Get audit log for a specific requester (who accessed what)."""
    audit = get_audit_log()
    scoped = _scoped_tenant(request)
    entries = audit.get_for_requester(requester_id)
    if scoped:
        entries = [e for e in entries if e.get("tenant_id", "default") == scoped]
    return {
        "requester_id": requester_id,
        "entries": entries,
    }


# ---- Sensitivity Detection ----


class SensitivityRequest(BaseModel):
    content: str = Field(..., min_length=1)


class SensitivityResponse(BaseModel):
    suggested_level: str
    confidence: float
    matched_signals: list[str]
    matched_categories: list[str]
    contains_hr_data: bool
    contains_financial_data: bool
    contains_client_data: bool
    contains_legal_data: bool
    contains_pii: bool
    is_sensitive: bool


@app.post("/api/v1/detect-sensitivity", response_model=SensitivityResponse)
async def detect_sensitivity_endpoint(req: SensitivityRequest):
    """Analyze content for confidential/sensitive signals.

    Returns the suggested sensitivity level and which patterns matched.
    Use this before saving to preview what classification will be applied.
    """
    result = detect_sensitivity(req.content)
    return SensitivityResponse(
        suggested_level=result.suggested_level.value,
        confidence=round(result.confidence, 3),
        matched_signals=result.matched_signals[:10],
        matched_categories=result.matched_categories,
        contains_hr_data=result.contains_hr_data,
        contains_financial_data=result.contains_financial_data,
        contains_client_data=result.contains_client_data,
        contains_legal_data=result.contains_legal_data,
        contains_pii=result.contains_pii,
        is_sensitive=result.is_sensitive,
    )


# ---- Main ----

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
