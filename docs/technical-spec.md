# ThreadWeave Technical Specification

> **Version:** 0.3.0 — Implementation
> **Status:** Active (updated 2026-08-01)

## 1. System Overview

ThreadWeave is an enterprise organizational memory system that wraps MemPalace for
storage and retrieval, adding org-awareness, one-click knowledge capture, and
relevance routing.

### 1.1 Core Insight

Knowledge is already being written — in emails, DMs, Slack threads, PR comments.
It's just locked in 1:1 channels. ThreadWeave promotes private expertise to
organizational memory with one click at the moment of writing.

### 1.2 Architecture Layers

```
┌────────────────────────────────────────────────┐
│ L4: ACCESS                                     │
│ MCP Server · Web Dashboard · Chat Bot · REST   │
├────────────────────────────────────────────────┤
│ L3: RELEVANCE ENGINE                           │
│ Semantic + Org proximity + Freshness + Authority│
├────────────────────────────────────────────────┤
│ L2: STORAGE (MemPalace, unchanged)             │
│ Vector Store · Knowledge Graph · Palace Graph  │
├────────────────────────────────────────────────┤
│ L1: SAVE FLOW                                  │
│ Review & Sanitize · Scope · Auto-Enrich        │
├────────────────────────────────────────────────┤
│ L0: INGESTION                                  │
│ Email · Slack/Teams · Browser · API · CLI      │
└────────────────────────────────────────────────┘
```

---

## 2. Data Models

### 2.1 Knowledge Entry (Drawer)

The core unit of captured knowledge. Stored verbatim in MemPalace.

```python
@dataclass
class KnowledgeEntry:
    id: str                    # MemPalace drawer ID
    content: str               # Verbatim original text (PII-stripped)
    closet: str                # Auto-generated summary (~200 chars)
    wing: str                  # Team that produced it (MemPalace Wing)
    room: str                  # Topic (MemPalace Room)
    hall: str                  # Memory type: facts, events, decisions, etc.
    source_type: str           # email, slack, pr_comment, manual, api
    source_url: str            # Link back to original (if available)
    author_id: str             # Person entity ID
    author_team: str           # Team at time of writing
    created_at: datetime       # When written
    captured_at: datetime      # When saved to ThreadWeave
    verified_at: datetime      # Last human verification
    scope: str                 # team, department, organization
    entities: list[EntityRef]  # Linked entities (systems, people, tech)
    triggers: list[str]        # Contexts that make this relevant
    tags: list[str]            # User-defined tags
```

### 2.2 Org Model (Knowledge Graph Triples)

Temporal entity-relationship triples in MemPalace Knowledge Graph (SQLite).

```
(Person,   member_of,        Team)      [valid_from → valid_to]
(Person,   reports_to,       Person)    [valid_from → valid_to]
(Team,     owns,             Domain)    [valid_from → valid_to]
(Team,     collaborates_with, Team)     [permanent]
(Team,     reports_to,       Person)    [valid_from → valid_to]
(Team,     subteam_of,       Team)      [valid_from → valid_to]
(Person,   authored,         DrawerID)  [permanent]
```

### 2.3 Detection Result

Output from the detection engine when analyzing raw text.

```python
@dataclass
class DetectionResult:
    content_type: ContentType   # answer, decision, question, chat, reference
    confidence: float           # 0.0 - 1.0
    signals: list[str]          # Which patterns fired
    entities: list[EntityRef]   # Extracted entities
    suggested_scope: str        # team, department, organization
    suggested_title: str        # Auto-generated title
    has_pii: bool               # PII detected?
```

### 2.4 Relevance Score

How search results are ranked for a specific requester.

```python
@dataclass
class RelevanceScore:
    semantic: float       # 0.0-1.0, MemPalace vector similarity
    org_proximity: float  # 0.0-1.0, same team at time = 1.0
    freshness: float      # 0.0-1.0, exponential decay
    authority: float      # 0.0-1.0, expert = high
    combined: float       # Weighted: 40% sem + 30% org + 20% fresh + 10% auth
```

---

## 3. API Contracts

### 3.1 REST API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/detect` | Analyze text for knowledge potential |
| POST | `/api/v1/entries` | Save a knowledge entry |
| GET | `/api/v1/entries/{id}` | Get a single entry |
| GET | `/api/v1/search?q=...` | Search with relevance ranking |
| GET | `/api/v1/wings` | List teams (MemPalace wings) |
| GET | `/api/v1/wings/{id}/rooms` | List topics in a team |
| POST | `/api/v1/org/relationships` | Add org relationship |
| GET | `/api/v1/org/people/{id}/team?as_of=...` | Get team at point in time |
| GET | `/api/v1/health` | Health check |

### 3.2 MCP Tools (via MemPalace)

All 35 MemPalace MCP tools are available unchanged. ThreadWeave adds:

| Tool | Description |
|------|-------------|
| `threadweave_detect` | Analyze text for saving potential |
| `threadweave_save` | Save knowledge entry with scope & enrichment |
| `threadweave_search_ranked` | Search with org-aware relevance ranking |
| `threadweave_org_query` | Query org model at point in time |
| `threadweave_stale_check` | Flag potentially outdated knowledge |

### 3.3 Webhook Integrations

| Integration | Trigger | Action |
|-------------|---------|--------|
| Slack | `:threadweave:` reaction | Detect → offer save |
| Teams | `/threadweave` command | Save current message |
| Email | BCC to `save@threadweave.company.com` | Detect → auto-save if high confidence |
| Browser ext | Highlight text → right-click | Detect → offer save |

---

## 4. Integration Points with MemPalace

### 4.1 What We Use (Unchanged)

| MemPalace Component | ThreadWeave Use |
|--------------------|-----------------|
| `searcher.search_memories()` | Base semantic search |
| `KnowledgeGraph` (SQLite) | Org model storage |
| `palace_graph` (Wings/Rooms/Tunnels) | Team/topic structure |
| MCP Server (35 tools) | AI agent access |
| ChromaDB / Qdrant / pgvector | Vector storage |
| `layers.py` (L0-L3) | Tiered memory loading |
| `hallways.py` | Cross-team connections |

### 4.2 What We Add (On Top)

| Component | Description |
|-----------|-------------|
| `detector.py` | Content classification |
| `org_model.py` | Temporal org structure |
| `relevance.py` | Re-ranking with org context |
| `mempalace_client.py` | Programmatic MemPalace access |
| REST API (FastAPI) | Human + integration access |
| Org-focused MCP tools | Extended agent tools |

### 4.3 Why Not Fork MemPalace

- Release cadence: v2.0.0 → v3.5.0 in 3 months (releases every few days)
- Forking means constant rebase pain
- The API surface is stable and well-defined
- The pluggable backend interface (RFC 001) supports Qdrant/pgvector natively
- MemPalace already has 35 MCP tools — we add 5 more, not rewrite

---

## 5. Deployment Architecture

### 5.1 Docker Compose (SMB)

```
┌─────────────────────────────────────┐
│ docker-compose.yml                  │
│                                     │
│  qdrant:        vector store        │
│  mempalace-mcp: MCP server          │
│  threadweave:   API + relevance     │
│                                     │
│  All on one machine, behind firewall│
└─────────────────────────────────────┘
```

### 5.2 Kubernetes (Enterprise)

```
┌──────────────────────────────────────────┐
│ Namespace: threadweave                   │
│                                          │
│  qdrant (StatefulSet, 3 replicas)        │
│  mempalace-mcp (Deployment, 2 replicas)  │
│  threadweave-api (Deployment, 3 pods)    │
│  redis (cache)                           │
│  postgres (if using pgvector backend)    │
│                                          │
│  Ingress → SSO (LDAP/SAML/OIDC)          │
│  Secrets → Vault / sealed-secrets        │
└──────────────────────────────────────────┘
```

### 5.3 Air-Gapped / On-Prem

- Embedding model runs locally (MemPalace ships `embeddinggemma-300m`)
- No external API calls at any stage
- Qdrant is self-hosted, no cloud dependency
- SQLite for knowledge graph, no external DB required

---

## 6. Security & Privacy

### 6.1 PII Handling

- Detection engine flags potential PII before save
- Save flow shows what will be saved, with PII highlighted
- User must confirm before PII-sensitive content is saved
- Option to auto-redact known PII patterns

### 6.2 Access Control

- RBAC at team (Wing) level
- Knowledge scoped: team-only, department, organization
- SSO integration (LDAP/SAML/OIDC)
- Audit log: who searched for what, when

### 6.3 Data Residency

- All data stays on-prem
- No telemetry or usage data leaves the network
- Configurable data retention policies

---

## 7. Open Questions

1. **HRIS integration depth:** Auto-sync from Workday/BambooHR, or manual org model?
2. **Detection engine accuracy:** Heuristic-only vs. add lightweight LLM rerank?
3. **MemPalace API stability:** The project moves fast. Integration tests are critical.
4. **First industry vertical:** Law firms, consulting, or engineering teams?
5. **Business model:** Open-core + enterprise license? Or pure implementation/consultancy?
