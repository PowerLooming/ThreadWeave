# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
ThreadWeave API — FastAPI server for organizational memory.

Central ingestion pipeline: connectors -> ingest -> detect -> store.
Multi-tenant aware. Content deduplication. PII filtering.
"""

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import os

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

app = FastAPI(
    title="ThreadWeave API",
    description="Enterprise organizational memory system with central ingestion pipeline",
    version="0.2.0",
)

# Auth middleware (no-op unless THREADWEAVE_REQUIRE_AUTH=true)
app.add_middleware(APIKeyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.on_event("startup")
async def startup():
    global _mempalace_available
    _mempalace_available = _mempalace.available
    if _mempalace_available:
        import logging
        logging.getLogger("threadweave.api").info(
            "MemPalace hybrid search available at %s", _mempalace.palace_path
        )


# ---- Health ----

@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="healthy",
        version="0.2.0",
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
    detector_mode = "regex"  # default; updated after detection
    # 1. Dedup — hash content + key metadata to avoid false dedup
    # when two emails share a body (templates) but have different subjects/senders
    t0 = time.monotonic()
    title = req.metadata.get("title", "")
    author = req.metadata.get("author_id", "")
    dedup_key = f"{req.content}|{title}|{author}"
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
    _dedup_hashes.add(content_hash)
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
    entry_id = str(uuid.uuid4())[:8]
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
        "author_id": req.metadata.get("author_id", "unknown"),
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

    # Store in tenant-isolated store
    tenant_store = _tenant_stores.setdefault(req.tenant_id, {})
    tenant_store[entry_id] = entry
    _memory_store[entry_id] = entry  # Also global for search

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
            )
        except Exception:
            pass  # In-memory fallback is sufficient
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
async def list_tenant_entries(tenant_id: str):
    """List all entries for a specific tenant."""
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
async def save_entry(req: SaveRequest):
    entry_id = str(uuid.uuid4())[:8]
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
            )
        except Exception:
            pass

    return SaveResponse(
        id=entry_id, wing=req.wing, room=req.room,
        title=entry["title"], created_at=now,
    )


# ---- Get Entry ----

@app.get("/api/v1/entries/{entry_id}", response_model=EntryResponse)
async def get_entry(entry_id: str, request: Request):
    entry = _memory_store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # Confidentiality enforcement
    requester = RequesterContext()  # Default: internal clearance
    if not requester.can_see(entry):
        audit = get_audit_log()
        audit.log_denied(requester, entry, "Insufficient clearance for direct access")
        raise HTTPException(status_code=403, detail="Access denied")

    # Audit: log access to sensitive entries
    audit = get_audit_log()
    audit.log_access(requester, entry, action="view")

    return EntryResponse(
        id=entry["id"], content=entry["content"], wing=entry["wing"],
        room=entry["room"], scope=entry["scope"],
        source_type=entry["source_type"], author_id=entry["author_id"],
        created_at=entry["created_at"], entities=entry["entities"],
    )


# ---- Search (MemPalace hybrid + keyword fallback, tenant-aware, confidentiality-filtered) ----

@app.post("/api/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request):
    """Search organizational memory.

    Uses MemPalace hybrid search (BM25 + vector cosine) when available,
    falling back to keyword matching in the in-memory store.

    Wing/room filters narrow results to a specific team/topic.
    Confidentiality: Results are filtered based on requester clearance.
    """
    query_lower = req.query.lower()
    results = []
    seen_ids: set[str] = set()

    # Build requester context for access enforcement
    requester = RequesterContext(
        person_id=req.requester_team or "",  # Use requester_team as person_id for now
        wing=req.requester_team or "",
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
                seen_ids.add(mr.drawer_id)
                results.append({
                    "id": mr.drawer_id,
                    "title": "",
                    "wing": mr.wing,
                    "room": mr.room,
                    "content_preview": mr.content[:200],
                    "created_at": mr.created_at,
                    "author_team": mr.wing,
                    "relevance_score": round(mr.similarity, 3),
                    "bm25_score": mr.bm25_score,
                    "source": "mempalace",
                    "sensitivity": "internal",  # MemPalace doesn't store sensitivity
                })
        except Exception as exc:
            logger = __import__("logging").getLogger("threadweave.api")
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
    for entry_id in denied_ids:
        entry = _memory_store.get(entry_id, {})
        audit.log_denied(requester, entry, "Insufficient clearance")

    # Sort by relevance
    visible.sort(key=lambda r: r["relevance_score"], reverse=True)
    visible = visible[:req.limit]

    return SearchResponse(
        results=visible, total=len(visible), query=req.query,
    )


# ---- Wings (Teams) ----

@app.get("/api/v1/wings")
async def list_wings():
    wings = {}
    for entry in _memory_store.values():
        wing = entry["wing"]
        if wing not in wings:
            wings[wing] = 0
        wings[wing] += 1
    return [{"name": name, "entry_count": count}
            for name, count in sorted(wings.items())]


# ---- Rooms (Topics) ----

@app.get("/api/v1/wings/{wing}/rooms")
async def list_rooms(wing: str):
    rooms = {}
    for entry in _memory_store.values():
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

# ---- Audit Log ----


@app.get("/api/v1/audit/recent")
async def get_audit_recent(limit: int = Query(default=50, ge=1, le=500)):
    """Get recent audit log entries for sensitive content access."""
    audit = get_audit_log()
    return {
        "entries": audit.get_recent(limit),
        "total": audit.count,
    }


@app.get("/api/v1/audit/entry/{entry_id}")
async def get_audit_for_entry(entry_id: str):
    """Get audit log for a specific knowledge entry."""
    audit = get_audit_log()
    return {
        "entry_id": entry_id,
        "entries": audit.get_for_entry(entry_id),
    }


@app.get("/api/v1/audit/requester/{requester_id}")
async def get_audit_for_requester(requester_id: str):
    """Get audit log for a specific requester (who accessed what)."""
    audit = get_audit_log()
    return {
        "requester_id": requester_id,
        "entries": audit.get_for_requester(requester_id),
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
