# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""ThreadWeave MCP server — organizational memory as agent tools.

Exposes ThreadWeave to any Model Context Protocol client over Streamable
HTTP: Copilot Studio agents, Microsoft 365 Copilot (federated connectors),
GitHub Copilot, Claude Code, or a plain MCP client.

Design: this module is a thin, stateless shim over the REST API. Every tool
call is an HTTP request to ThreadWeave carrying the caller's own API key, so
tenant scoping, confidentiality ACLs, the gossip gate, sensitivity detection
and the audit trail all stay in the one place that already owns them. No
memory logic is duplicated here, and an agent read is audited exactly like a
human read.

Run it:
    threadweave mcp                              # http://127.0.0.1:8100/mcp
    threadweave mcp --host 0.0.0.0 --port 8100

Environment:
    THREADWEAVE_API_BASE_URL   REST API base URL (default http://127.0.0.1:8000)
    THREADWEAVE_API_KEY        API key sent when the *client* does not send one
    THREADWEAVE_REQUIRE_AUTH   same opt-in auth gate as the REST API
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import threadweave.auth as auth

logger = logging.getLogger("threadweave.mcp")

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100
MCP_PATH = "/mcp"
DEFAULT_TIMEOUT = 30.0

# Tool hints: agents use these to decide whether a call needs confirmation.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False,
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False,
    openWorldHint=False,
)

INSTRUCTIONS = """\
ThreadWeave is this organization's memory: decisions, commitments and tacit
knowledge captured from the tools people already work in (email, Teams,
SharePoint, drive), with provenance back to the exact source.

Use search_memory or get_decisions before answering questions about what the
organization decided, why, or who owns what, then cite the entry id and the
source_url it returns. Use list_open_commitments for outstanding action
items. Use classify_text before capture_knowledge when you are unsure whether
a statement is knowledge at all. Use capture_knowledge to write down what you
and the user concluded, so the next agent session does not have to rediscover
it.

Reads are confidentiality-filtered for the caller's key and are audited.
Gossip and hearsay are refused at capture; do not store them.
"""


# ── API client plumbing ─────────────────────────────────────────────


def _default_api_base_url() -> str:
    return os.environ.get("THREADWEAVE_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")


def _default_client_factory() -> httpx.AsyncClient:
    """A fresh client per call.

    Per-call clients keep the tools free of shared event-loop state (the same
    server is used from uvicorn, from tests, and potentially embedded), and
    the REST API is local, so the connection-reuse saving is not worth the
    coupling.
    """
    return httpx.AsyncClient(base_url=_api_base_url, timeout=_timeout)


_api_base_url: str = _default_api_base_url()
_timeout: float = DEFAULT_TIMEOUT
_client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory


def configure(api_base_url: Optional[str] = None, timeout: Optional[float] = None) -> None:
    """Point the tools at a ThreadWeave REST API."""
    global _api_base_url, _timeout
    if api_base_url:
        _api_base_url = api_base_url.rstrip("/")
    if timeout is not None:
        _timeout = float(timeout)


def set_client_factory(factory: Optional[Callable[[], httpx.AsyncClient]]) -> None:
    """Override how tool calls reach the REST API (tests, embedding)."""
    global _client_factory
    _client_factory = factory or _default_client_factory


def reset() -> None:
    """Restore default settings and client factory."""
    global _api_base_url, _timeout, _client_factory
    _api_base_url = _default_api_base_url()
    _timeout = DEFAULT_TIMEOUT
    _client_factory = _default_client_factory


def api_base_url() -> str:
    return _api_base_url


def _agent_key(ctx: Context) -> str:
    """The API key the MCP caller presented, if any.

    MCP header names arrive lower-cased; the key is forwarded verbatim so the
    REST layer sees the same identity the MCP layer authenticated.
    """
    headers = {str(k).lower(): str(v) for k, v in (getattr(ctx, "headers", None) or {}).items()}
    key = headers.get("x-api-key", "").strip()
    if key:
        return key
    bearer = headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return os.environ.get("THREADWEAVE_API_KEY", "").strip()


def _error_from_response(resp: httpx.Response) -> dict:
    detail: Any
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except Exception:
        detail = resp.text[:400]
    return {
        "error": f"ThreadWeave API returned HTTP {resp.status_code}",
        "status": resp.status_code,
        "detail": detail,
    }


async def _api(
    ctx: Context,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
) -> dict:
    """Call the ThreadWeave REST API, returning a dict either way.

    HTTP-level failures come back as ``{"error": ...}`` so the agent can read
    and report them. A transport failure raises, because the tool did not run
    at all: the MCP client then sees isError instead of an empty answer that
    looks like "no knowledge found".
    """
    key = _agent_key(ctx)
    headers = {"X-API-Key": key} if key else {}
    try:
        async with _client_factory() as client:
            resp = await client.request(
                method, path,
                params={k: v for k, v in (params or {}).items() if v not in (None, "")},
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"ThreadWeave API unreachable at {_api_base_url} ({exc.__class__.__name__}). "
            "Start it with 'threadweave serve' or set THREADWEAVE_API_BASE_URL."
        ) from exc
    if resp.status_code >= 400:
        return _error_from_response(resp)
    try:
        return resp.json()
    except Exception:
        return {"error": "ThreadWeave API returned a non-JSON body", "status": resp.status_code}


# ── Shared query helpers ────────────────────────────────────────────


def _trim_hit(hit: dict) -> dict:
    return {
        "id": hit.get("id", ""),
        "title": hit.get("title", ""),
        "wing": hit.get("wing", ""),
        "room": hit.get("room", ""),
        "content_type": hit.get("content_type", "unknown"),
        "content": hit.get("content_preview", ""),
        "created_at": hit.get("created_at", ""),
        "author_id": hit.get("author_id", ""),
        "relevance_score": hit.get("relevance_score", 0),
        "sensitivity": hit.get("sensitivity", "internal"),
        "source_url": hit.get("source_url", ""),
    }


async def _search(
    ctx: Context,
    query: str,
    *,
    wing: str = "",
    room: str = "",
    content_type: str = "",
    limit: int = 10,
) -> dict:
    """Search memory, optionally narrowing to one content type.

    The type filter is applied here rather than server-side: a hit whose type
    is not reported (older entries, engine gaps) is resolved through its own
    entry record instead of being dropped, so a typed query cannot silently
    return "nothing" while matches exist.
    """
    want = (content_type or "").strip().lower()
    fetch = max(1, min(limit, 50))
    payload = {
        "query": query,
        "limit": min(50, fetch * 3) if want else fetch,
    }
    if wing:
        payload["wing"] = wing
    if room:
        payload["room"] = room

    data = await _api(ctx, "POST", "/api/v1/search", payload=payload)
    if "error" in data:
        return data

    hits = data.get("results", []) or []
    if want:
        resolved: list[dict] = []
        unknown = 0
        for hit in hits:
            ctype = (hit.get("content_type") or "").lower()
            if not ctype or ctype == "unknown":
                if unknown >= fetch:
                    continue
                unknown += 1
                entry = await _api(ctx, "GET", f"/api/v1/entries/{hit.get('id', '')}")
                if "error" in entry:
                    continue
                ctype = (entry.get("content_type") or "").lower()
                hit = {**hit, "content_type": ctype or "unknown"}
            if ctype == want:
                resolved.append(hit)
        hits = resolved

    trimmed = [_trim_hit(h) for h in hits[:fetch]]
    return {
        "query": query,
        "total": len(trimmed),
        "results": trimmed,
        "note": (
            "Results are already filtered to what the calling key is cleared "
            "to see; a smaller total than expected can mean denied content, "
            "not absent content. source_url is the citation back to the "
            "captured source."
        ),
    }


# ── Server ──────────────────────────────────────────────────────────


def _server_version() -> str:
    """The ThreadWeave version (single source: the API app)."""
    try:
        from threadweave.api import app as api_app

        return api_app.version
    except Exception:  # pragma: no cover - import-time only
        return ""


def build_server() -> MCPServer:
    """Build the MCP server with the ThreadWeave tool surface."""
    server = MCPServer(
        name="threadweave",
        title="ThreadWeave organizational memory",
        description=(
            "Search, cite and extend this organization's memory: decisions, "
            "commitments and tacit knowledge captured from email, Teams, "
            "SharePoint and documents, confidentiality-filtered for the "
            "calling key."
        ),
        instructions=INSTRUCTIONS,
        version=_server_version(),
    )

    @server.tool(
        name="search_memory",
        annotations=READ_ONLY,
        description=(
            "Search organizational memory for decisions, answers, references "
            "and how-things-work notes. Returns entry ids, short previews, "
            "content types, sensitivity and a citation URL back to the "
            "captured source. Narrow with wing (team) or room (topic), or set "
            "content_type to 'decision' for decisions only."
        ),
    )
    async def search_memory(
        ctx: Context,
        query: str,
        wing: str = "",
        room: str = "",
        content_type: str = "",
        limit: int = 10,
    ) -> dict:
        return await _search(
            ctx, query, wing=wing, room=room,
            content_type=content_type, limit=limit,
        )

    @server.tool(
        name="get_decisions",
        annotations=READ_ONLY,
        description=(
            "Answer 'what did we decide' questions. Returns only entries the "
            "classifier typed as decisions, each with who said it, when, and "
            "the citation URL. Use this instead of search_memory when the "
            "question is about a decision, a choice, or a rejected option."
        ),
    )
    async def get_decisions(
        ctx: Context,
        query: str,
        wing: str = "",
        limit: int = 10,
    ) -> dict:
        return await _search(ctx, query, wing=wing, content_type="decision", limit=limit)

    @server.tool(
        name="get_entry",
        annotations=READ_ONLY,
        description=(
            "Read one memory entry in full by id: content, scope, author, "
            "entities and creation time. Use after a search when the preview "
            "is not enough to answer or cite."
        ),
    )
    async def get_entry(ctx: Context, entry_id: str) -> dict:
        entry = await _api(ctx, "GET", f"/api/v1/entries/{entry_id}")
        if "error" in entry:
            return entry
        return {
            "id": entry.get("id", entry_id),
            "content": entry.get("content", ""),
            "wing": entry.get("wing", ""),
            "room": entry.get("room", ""),
            "scope": entry.get("scope", ""),
            "source_type": entry.get("source_type", ""),
            "author_id": entry.get("author_id", ""),
            "created_at": entry.get("created_at", ""),
            "entities": entry.get("entities", []),
            "version_of": entry.get("version_of", ""),
            "refinement_notes": entry.get("refinement_notes", []),
        }

    @server.tool(
        name="entry_provenance",
        annotations=READ_ONLY,
        description=(
            "Trace where a memory entry came from: its version chain and the "
            "audit trail of who accessed it. Use before relying on a "
            "surprising entry, or when answering 'how do we know this'."
        ),
    )
    async def entry_provenance(ctx: Context, entry_id: str) -> dict:
        versions = await _api(ctx, "GET", f"/api/v1/entries/{entry_id}/versions")
        if "error" in versions:
            return versions
        audit = await _api(ctx, "GET", f"/api/v1/audit/entry/{entry_id}")
        chain = versions.get("versions", []) or []
        return {
            "entry_id": entry_id,
            "version_chain": chain,
            "is_latest": bool(chain) and chain[-1].get("id") == entry_id,
            "access_audit": (audit.get("entries", []) if "error" not in audit else []),
            "note": (
                "Access audit lists sensitive-content reads for this entry "
                "(who, when, why), which is what makes agent access "
                "reviewable."
            ),
        }

    @server.tool(
        name="who_knows",
        annotations=READ_ONLY,
        description=(
            "Find who in the organization has captured knowledge about a "
            "topic: authors and teams whose entries match, with counts. "
            "Derived from memory, not from the directory, so it reflects "
            "who has actually written about the topic."
        ),
    )
    async def who_knows(ctx: Context, topic: str, wing: str = "", limit: int = 10) -> dict:
        found = await _search(ctx, topic, wing=wing, limit=max(limit * 2, 10))
        if "error" in found:
            return found

        people: dict[str, dict] = {}
        wings: dict[str, int] = {}
        for hit in found.get("results", []):
            author = hit.get("author_id") or ""
            if author:
                person = people.setdefault(
                    author, {"author_id": author, "entries": 0, "teams": [], "latest": ""}
                )
                person["entries"] += 1
                team = hit.get("wing") or ""
                if team and team not in person["teams"]:
                    person["teams"].append(team)
                if not person["latest"]:
                    person["latest"] = hit.get("content", "")[:160]
            team = hit.get("wing") or ""
            if team:
                wings[team] = wings.get(team, 0) + 1

        ranked = sorted(people.values(), key=lambda p: p["entries"], reverse=True)[:limit]
        team_counts = sorted(wings.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "topic": topic,
            "people": ranked,
            "teams": [{"wing": wing_name, "entries": n} for wing_name, n in team_counts],
            "note": "Counts are per visible entry; denied content is not counted.",
        }

    @server.tool(
        name="list_open_commitments",
        annotations=READ_ONLY,
        description=(
            "List action items captured from conversations: what was promised, "
            "by whom, with any deadline and status. Use for 'what is "
            "outstanding', 'what did we promise', 'who owes what'. Filter by "
            "owner id or display name."
        ),
    )
    async def list_open_commitments(ctx: Context, owner: str = "", status: str = "open") -> dict:
        data = await _api(ctx, "GET", "/api/v1/tasks", params={"owner": owner, "status": status})
        if "error" in data:
            return data
        tasks = data.get("tasks", []) or []
        return {
            "tasks": tasks,
            "count": len(tasks),
            "status_filter": status,
            "note": (
                "deadline is empty when the conversation carried no date; "
                "owner_resolved=false means the owner name was not matched to "
                "an identity yet."
            ),
        }

    @server.tool(
        name="classify_text",
        annotations=READ_ONLY,
        description=(
            "Pre-flight check before capturing text: is this knowledge at all, "
            "what type is it, how confident, does it carry gossip, PII "
            "references, a language other than English, and what sensitivity "
            "level would it be stored at. Gossip is refused at capture, so "
            "classify first when unsure."
        ),
    )
    async def classify_text(ctx: Context, text: str) -> dict:
        detected = await _api(ctx, "POST", "/api/v1/detect", payload={"text": text})
        if "error" in detected:
            return detected
        sensitivity = await _api(ctx, "POST", "/api/v1/detect-sensitivity", payload={"content": text})
        return {
            "should_save": detected.get("should_save"),
            "content_type": detected.get("content_type"),
            "confidence": detected.get("confidence"),
            "signals": detected.get("signals", []),
            "entities": detected.get("entities", []),
            "has_gossip": detected.get("has_gossip"),
            "has_pii": detected.get("has_pii"),
            "language": detected.get("language", ""),
            "suggested_scope": detected.get("suggested_scope", ""),
            "suggested_title": detected.get("suggested_title", ""),
            "sensitivity": (
                {
                    "level": sensitivity.get("suggested_level"),
                    "confidence": sensitivity.get("confidence"),
                    "matched_categories": sensitivity.get("matched_categories", []),
                    "is_sensitive": sensitivity.get("is_sensitive"),
                }
                if "error" not in sensitivity else {}
            ),
            "note": (
                "should_save=false usually means chat or noise; has_gossip=true "
                "means capture will be refused; a non-English language is "
                "translated at ingest and stays searchable in both languages."
            ),
        }

    @server.tool(
        name="capture_knowledge",
        annotations=WRITE,
        description=(
            "Write a decision, commitment, answer or how-things-work note into "
            "organizational memory, so the next session (human or agent) does "
            "not have to rediscover it. The server still applies its own "
            "gossip gate, sensitivity detection and opt-out registry. Include "
            "the source of the statement in the content."
        ),
    )
    async def capture_knowledge(
        ctx: Context,
        content: str,
        wing: str,
        room: str = "general",
        scope: str = "team",
        title: str = "",
        source_type: str = "agent",
        author_id: str = "agent:mcp",
    ) -> dict:
        payload = {
            "content": content,
            "wing": wing,
            "room": room,
            "scope": scope,
            "title": title,
            "source_type": source_type,
            "author_id": author_id,
        }
        data = await _api(ctx, "POST", "/api/v1/entries", payload=payload)
        if data.get("status") == 422 and "gossip" in str(data.get("detail", "")).lower():
            return {
                "saved": False,
                "reason": "rejected_gossip",
                "detail": data.get("detail"),
                "note": "Gossip and personal attacks are never stored, by design.",
            }
        if "error" in data:
            return data
        return {
            "saved": True,
            "id": data.get("id", ""),
            "wing": data.get("wing", wing),
            "room": data.get("room", room),
            "title": data.get("title", ""),
            "created_at": data.get("created_at", ""),
            "note": (
                "Stored with the caller's tenant and the server's detected "
                "sensitivity. It is searchable immediately; a human can revoke "
                "or refine it later."
            ),
        }

    @server.tool(
        name="browse_topics",
        annotations=READ_ONLY,
        description=(
            "Browse memory by topic instead of searching: the subjects that "
            "entries cluster into, with sizes. Use it to orient in a memory "
            "you do not know yet, then search within a topic. Only topics made "
            "of entries the calling key may see are returned."
        ),
    )
    async def browse_topics(ctx: Context, min_size: int = 2, max_topics: int = 20) -> dict:
        data = await _api(
            ctx, "POST", "/api/v1/topics",
            payload={"min_size": min_size, "max_topics": max_topics},
        )
        if "error" in data:
            return data
        topics = data.get("topics", []) or []
        return {
            "topics": topics,
            "total_entries": data.get("total_entries", 0),
            "note": "Topic labels come from entities and title keywords in visible entries.",
        }

    return server


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Require a valid ThreadWeave API key on the MCP endpoint.

    Same opt-in gate as the REST API (THREADWEAVE_REQUIRE_AUTH). The key is
    validated here and then forwarded by each tool, so the MCP surface and
    the REST surface cannot disagree about who is calling.
    """

    async def dispatch(self, request: Request, call_next):
        if not auth.AUTH_ENABLED:
            return await call_next(request)

        raw = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "")
            .removeprefix("Bearer ")
            .removeprefix("bearer ")
            .strip()
        )
        if not raw:
            return JSONResponse({"error": "Missing API key"}, status_code=401)
        info = auth.validate_key(raw)
        if info is None:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)
        request.state.mcp_tenant_id = info.tenant_id
        request.state.mcp_role = info.role
        return await call_next(request)


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def transport_security_settings(
    allowed_hosts: Optional[list[str]] = None,
    allowed_origins: Optional[list[str]] = None,
) -> Optional[TransportSecuritySettings]:
    """Host/Origin allowlist for the MCP endpoint.

    Without this the SDK admits only loopback hosts, so a published endpoint
    (Copilot Studio, Agent 365, APIM, a company DNS name) answers 421 to
    every request and the agent reports a broken tool. Set
    THREADWEAVE_MCP_ALLOWED_HOSTS (and, if a browser origin calls it,
    THREADWEAVE_MCP_ALLOWED_ORIGINS) to the names this server answers to,
    e.g. "mcp.example.com,mcp.example.com:443".
    """
    hosts = allowed_hosts if allowed_hosts is not None else _env_list("THREADWEAVE_MCP_ALLOWED_HOSTS")
    origins = (
        allowed_origins if allowed_origins is not None
        else _env_list("THREADWEAVE_MCP_ALLOWED_ORIGINS")
    )
    if not hosts and not origins:
        # None keeps the SDK's loopback-only default: safe for local use.
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def streamable_http_app(
    server: Optional[MCPServer] = None,
    mcp_path: str = MCP_PATH,
    allowed_hosts: Optional[list[str]] = None,
    allowed_origins: Optional[list[str]] = None,
):
    """The ASGI app Copilot Studio or any MCP client points at."""
    server = server or build_server()
    app = server.streamable_http_app(
        streamable_http_path=mcp_path,
        transport_security=transport_security_settings(allowed_hosts, allowed_origins),
    )
    app.add_middleware(MCPAuthMiddleware)
    return app


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api_base_url: Optional[str] = None,
) -> None:
    """Run the MCP server (Streamable HTTP) with uvicorn."""
    import uvicorn

    configure(api_base_url=api_base_url)
    print(f"ThreadWeave MCP server: http://{host}:{port}{MCP_PATH}")
    print(f"REST API: {_api_base_url}")
    print(f"Auth: {'API key required (X-API-Key)' if auth.AUTH_ENABLED else 'open (development)'}")
    uvicorn.run(streamable_http_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    serve()
