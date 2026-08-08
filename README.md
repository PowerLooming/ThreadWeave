# ThreadWeave — Organizational Memory System

MIT License. Wraps MemPalace for enterprise organizational knowledge capture and retrieval.

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
threadweave detect "After evaluating three databases, we chose PostgreSQL for the new platform because JSONB and full-text search are critical for our workload, and the decision is documented."
# → {"should_save": true, "content_type": "decision", "confidence": 0.50, ...}

# Save knowledge manually
threadweave save --wing engineering --room postgres --content "Always use connection pooling with at least 20 connections"

# Search organizational memory
threadweave search "PostgreSQL"
# → 1. [engineering/postgres] Always use connection pooling...
```

## Ingesting Emails

Feed existing mailboxes into ThreadWeave without touching the API:

| Script | Source | Auth |
|---|---|---|
| `python ingest_imap.py --provider outlook --user you@company.com --max 100` | Any IMAP provider (Outlook, Gmail, custom) | Password or app password |
| `python ingest_graph_mail.py --max 100` | New Outlook / M365 via Graph API | Device-code sign-in, MFA supported, no Azure app |
| `python ingest_emails.py ~/Desktop/exported-emails/ --wing legal` | Exported `.eml` files | None |
| `python ingest_outlook.py --max 100 --wing work` | Classic Outlook via COM | None (Windows only) |

Gmail requires an app password. If your org disabled app passwords, use `ingest_graph_mail.py` instead. All scripts support `--dry-run` to preview before ingesting.

## Continuous Capture (daemons)

The M365 connectors run as continuous daemons that pull content one-way into on-prem ThreadWeave. No webhooks, no tunnels, no third-party relays — content flows outbound from the on-prem host only.

```bash
# Run in the foreground (development):
uv run python -m threadweave.cli email watch --mailbox Admin@your-tenant.com --interval 300
uv run python -m threadweave.cli sharepoint watch --interval 300 --site "Mark 8" --onenote
uv run python -m threadweave.cli graph daemon

# Or manage them as OS services (start at login, survive reboots):
uv run python -m threadweave.cli daemon config email-watch \
  --set THREADWEAVE_EMAIL_MAILBOX=Admin@your-tenant.com
uv run python -m threadweave.cli daemon config teams-bot \
  --set MICROSOFT_APP_ID=... MICROSOFT_APP_PASSWORD=...
uv run python -m threadweave.cli daemon install email-watch   # all four
uv run python -m threadweave.cli daemon status all
uv run python -m threadweave.cli daemon uninstall email-watch
```

**How packaging works:** per-daemon env files at `~/.threadweave/daemons/<name>.env` hold secrets and options (one place, not shell history); `daemon run <name>` loads the env and dispatches. **Windows:** a launcher `.cmd` is dropped into the Startup folder (no admin needed) with logs to `~/.threadweave/logs/`. **Linux:** systemd units with `Restart=always`. Daemon env options: `THREADWEAVE_DAEMON_INTERVAL`, `THREADWEAVE_EMAIL_MAILBOX`, `THREADWEAVE_SP_SITE`, `THREADWEAVE_SP_ONENOTE`, `THREADWEAVE_GRAPH_INTERVAL`, `PORT` (bot).

State files (`~/.threadweave/`) let daemons resume safely: SharePoint delta tokens, OneNote watermarks, MSAL token cache, opt-out registry, audit log, entry store, notifications.

## Privacy

Capture without disclosure is surveillance, so ThreadWeave ships a privacy layer: per-person **opt-out** (checked at ingest and in every daemon before extraction), **right to delete** (audited per-entry deletion), Teams commands `opt out` / `opt in` / `delete <topic>` / `status`, and a full audit trail. See **[docs/privacy.md](docs/privacy.md)**.

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /api/v1/ingest` | Central ingestion pipeline (dedup → detect → opt-out gate → store) |
| `POST /api/v1/search` | Hybrid search (MemPalace vector + keyword fallback) |
| `POST /api/v1/detect` | Classify text (answer/decision/question/chat) |
| `POST /api/v1/entries` | Save knowledge entry |
| `GET /api/v1/entries/{id}` | Retrieve entry |
| `DELETE /api/v1/entries/{id}` | Delete entry (author / same-wing / admin; audited) |
| `GET /api/v1/optout` | List opted-out people (privacy admin) |
| `POST /api/v1/optout/out` | Register opt-out |
| `POST /api/v1/optout/in` | Remove opt-out |
| `GET /api/v1/wings` | List teams/departments |
| `GET /api/v1/org/graph` | Org graph (nodes + edges for visualization) |
| `POST /api/v1/org/relationships` | Manage org structure |
| `GET /api/v1/org/people/{id}/team` | Get person's team at a point in time |
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

- [M365 Connector Setup](docs/m365-connectors.md) — Azure app registrations, email/SharePoint/OneNote daemons, Copilot connector, troubleshooting
- [Privacy Model](docs/privacy.md) — on-prem one-way contract, opt-out, right to delete, access control
- [Technical Specification](docs/technical-spec.md)

## Configuration

| Env Variable | Description |
|---|---|
| `MEMPALACE_PALACE_PATH` | MemPalace data directory (default: `~/.mempalace/palace/default`) |
| `THREADWEAVE_LLM_API_KEY` | Enable LLM-based detection (falls back to regex) |
| `THREADWEAVE_LLM_BASE_URL` | Custom LLM endpoint (Ollama, vLLM, etc.) |
| `THREADWEAVE_LLM_MODEL` | Model name (default: gpt-4o-mini) |
| `THREADWEAVE_REQUIRE_AUTH` | Set to `1` to enable API key auth |
| `THREADWEAVE_API_KEYS` | `tenant:key,tenant:key` format. For roles/identity (admin, hr_admin, legal, wing, person_id) use `~/.threadweave/keys.json` instead |
| `THREADWEAVE_CORS_ORIGINS` | Comma-separated allowed origins (default: `*`). Restrict when exposed beyond local dev |
| `THREADWEAVE_AUDIT_DB` | Audit log database path (default: `~/.threadweave/audit.sqlite3`). Falls back to in-memory if the DB can't be opened |
| `THREADWEAVE_ENTRY_DB` | Entry store database URL. SQLite (default `sqlite:///~/.threadweave/entries.sqlite3`) works out of the box; PostgreSQL (`postgresql://user:***@host/db`, install `.[postgres]`) for corporate deployments. Entries survive API restarts |

**Connector extras:** `pip install -e ".[gws]"` (Google Workspace), `".[graph]"` (Microsoft Graph connector), `".[teams]"`, `".[sharepoint]"`, `".[email]"`, `".[outlook]"`, or `".[all-connectors]"` for everything.

## What's Built

- ✅ **Detection engine** — Regex + LLM two-tier classifier (ANSWER/DECISION/QUESTION/CHAT/REFERENCE)
- ✅ **PII detection** — International regex patterns (EN/NO/DE/FR/ES/IT) + LLM prompt hardening. Catches SSN, credit cards, IBAN, bank accounts, passport numbers, salary figures, home addresses, and medical data without false-flagging company names or workplace identifiers.
- ✅ **Ingestion pipeline** — Central dedup → detect → PII gate → store
- ✅ **MemPalace integration** — Hybrid search (BM25 + vector cosine)
- ✅ **Org model** — Full MemPalace Knowledge Graph integration with temporal triples. Team membership, reporting chains, relevant-people search, HRIS bulk sync. Dual-mode: with or without KG.
- ✅ **Hallway/Tunnel graph navigation** — D3.js force-directed graph visualization in the web dashboard. Click-to-highlight, drag-to-rearrange, wing filter, hallway (within-wing) vs tunnel (cross-wing) edge coloring.
- ✅ **Web dashboard** — Single-file SPA with pipeline overview, search, save, entries, and graph tabs.
- ✅ **API server** — FastAPI with auto-generated docs
- ✅ **CLI** — `detect`, `search`, `save`, `serve`
- ✅ **Confidentiality** — 7 sensitivity levels with access enforcement + audit
- ✅ **Google Workspace connector** — Gmail, Chat, Drive ingestion + offboarding harvester
- ✅ **Microsoft Graph connector** — Copilot integration via external connection
- ✅ **Profiling** — Latency percentiles, throughput, Prometheus export
- ✅ **Auth** — Opt-in API key middleware with tenant scoping
- ✅ **Docker** — Multi-stage build with optional Ollama profile
- ✅ **Teams bot connector** — @mention capture + passive detection with consent card, RSC group-chat capture, privacy commands (`opt out`, `opt in`, `delete <topic>`, `status`)
- ✅ **Email watch daemon** — continuous one-way mailbox polling, thread-aware capture, sender→department→wing mapping
- ✅ **SharePoint watch daemon** — delta-polling of document libraries (new + edited files), xlsx/pptx/docx/pdf extraction, OneNote notebook polling via delegated auth
- ✅ **Copilot connector** — Graph external connection, schema, item sync, continuous daemon
- ✅ **Privacy layer** — opt-out registry (ingest gate + early daemon skips), audited right-to-delete, Teams privacy commands
- ✅ **Durable entry store** — SQLite write-through (`~/.threadweave/entries.sqlite3`); the palace survives restarts
- ✅ **Entry versioning** — re-captured documents (same source_file) chain to the original; `GET /api/v1/entries/{id}/versions` shows the evolution
- ✅ **366 tests** — full suite green

## What's Next

- [ ] Entry versioning (edits link to the original instead of a second entry)
- [ ] Capture notification (Teams DM: "your email about X was added to the palace")
- [ ] Knowledge entry nodes in the graph (entries linked to org entities)
- [ ] Deployment packaging (systemd / Windows service wrappers for the daemons)
