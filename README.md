# ThreadWeave — Organizational Memory System

AGPL v3 or later. Wraps MemPalace for enterprise organizational knowledge capture and retrieval.

**Every thread, woven into memory.**

🌐 **[threadweave.net](https://threadweave.net)**

## Quick Start (Native Python)

```bash
# Prerequisites: Python 3.11+ and uv
# Install uv: https://docs.astral.sh/uv/getting-started/installation/

# Clone or copy this directory to your machine, then:
cd threadweave
bash setup.sh
```

Or manually:

```bash
uv venv --python 3.11 .venv
source .venv/Scripts/activate   # Windows
uv pip install -e ".[dev]"
```

## Quick Start (Docker)

```bash
# Clone the repo
git clone https://github.com/PowerLooming/ThreadWeave
cd ThreadWeave

# Start ThreadWeave + MemPalace
docker compose up

# → API: http://localhost:8000
# → API docs: http://localhost:8000/docs

# Optional: run with a local LLM for smarter detection
docker compose --profile llm up
# Pulls ollama + llama3.1:8b automatically on first start
```

Data persists in Docker volumes — your knowledge survives restarts.

## Usage

```bash
# Start the API server
threadweave serve
# → http://localhost:8000
# → API docs: http://localhost:8000/docs

# Analyze text for knowledge potential
threadweave detect "We decided to use PostgreSQL for the auth service"
# → {"should_save": true, "content_type": "decision", "confidence": 0.25, ...}

# Save knowledge manually
threadweave save --wing engineering --room postgres --content "Always use connection pooling with at least 20 connections"

# Search organizational memory
threadweave search "PostgreSQL"
# → 1. [engineering/postgres] Always use connection pooling...
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /api/v1/ingest` | Central ingestion pipeline (dedup → detect → store) |
| `POST /api/v1/search` | Hybrid search (MemPalace vector + keyword fallback) |
| `POST /api/v1/detect` | Classify text (answer/decision/question/chat) |
| `POST /api/v1/entries` | Save knowledge entry |
| `GET /api/v1/entries/{id}` | Retrieve entry |
| `GET /api/v1/wings` | List teams/departments |
| `POST /api/v1/org/relationships` | Manage org structure |
| `POST /api/v1/detect-sensitivity` | Auto-classify confidentiality |
| `GET /api/v1/health` | Health check |
| `GET /api/v1/metrics` | Pipeline metrics (JSON) |
| `GET /api/v1/metrics/prometheus` | Pipeline metrics (Prometheus) |
| `GET /api/v1/audit/recent` | Audit log |

## Architecture

```
[Teams] [Email] [SharePoint] [Drive] [Chat]     ← Connectors
    │        │         │           │       │
    └────────┼─────────┼───────────┼───────┘
             │  POST /api/v1/ingest
        ┌────▼─────────────────────┐
        │  Central Ingestion Pipe   │
        │  Dedup → Detect → Store   │
        └────┬─────────────────────┘
             │
        ┌────▼────┐  ┌──────────┐
        │MemPalace│  │Org Model │
        │Hybrid   │  │Temporal  │
        │Search   │  │KG        │
        └────┬────┘  └──────────┘
             │
        ┌────▼─────────────────────┐
        │  Relevance + Confid.      │
        │  Rank → Filter → Audit    │
        └──────────────────────────┘
```

## Documentation

- [M365 Connector Setup](docs/m365-connectors.md) — Azure app registration, Email Watcher, SharePoint Watcher, Copilot connector
- [Technical Specification](docs/technical-spec.md)

## Configuration

| Env Variable | Description |
|---|---|
| `MEMPALACE_PALACE_PATH` | MemPalace data directory (default: `~/.mempalace/palace/default`) |
| `THREADWEAVE_LLM_API_KEY` | Enable LLM-based detection (falls back to regex) |
| `THREADWEAVE_LLM_BASE_URL` | Custom LLM endpoint (Ollama, vLLM, etc.) |
| `THREADWEAVE_LLM_MODEL` | Model name (default: gpt-4o-mini) |
| `THREADWEAVE_REQUIRE_AUTH` | Set to `1` to enable API key auth |
| `THREADWEAVE_API_KEYS` | `tenant:key,tenant:key` format |

## What's Built

- ✅ **Detection engine** — Regex + LLM two-tier classifier (ANSWER/DECISION/QUESTION/CHAT/REFERENCE)
- ✅ **Ingestion pipeline** — Central dedup → detect → PII gate → store
- ✅ **MemPalace integration** — Hybrid search (BM25 + vector cosine)
- ✅ **API server** — FastAPI with auto-generated docs
- ✅ **CLI** — `detect`, `search`, `save`, `serve`
- ✅ **Confidentiality** — 7 sensitivity levels with access enforcement + audit
- ✅ **Org model** — Temporal knowledge graph for team/role/person relationships
- ✅ **Google Workspace connector** — Gmail, Chat, Drive ingestion + offboarding harvester
- ✅ **Microsoft Graph connector** — Copilot integration via external connection
- ✅ **Profiling** — Latency percentiles, throughput, Prometheus export
- ✅ **Auth** — Opt-in API key middleware with tenant scoping
- ✅ **Docker** — Multi-stage build with optional Ollama profile
- ✅ **274 tests, 0 failures**

## What's Next

- [ ] PII regex tuning (company names trigger false positives)
- [ ] Full MemPalace Knowledge Graph integration for org model
- [ ] Hallway/Tunnel graph navigation
- [ ] Web dashboard (static/index.html is a stub)
- [ ] Multi-user / RBAC
- [ ] SharePoint file watcher
- [ ] Teams bot connector
