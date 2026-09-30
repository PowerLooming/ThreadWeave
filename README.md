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

## Personal Profile (single user)

The same installation also runs as a single-user deployment: one mailbox, one owner, everything
on your machine. There is no separate build and no desktop app, only a narrower profile.

```bash
export THREADWEAVE_PROFILE=personal
threadweave email login                 # one-time device-code sign-in
threadweave email watch --interval 300  # reads your own mailbox over /me
```

In this profile the email watcher reads your own mailbox as you, with delegated access, so
there is no client secret and no application permission, and captures are filed under the
`personal` tenant. The connector set is the email watcher: the Teams, SharePoint and org-wide
harvesters are not selectable, which also means no RSC consent and no admin grants. What it
needs is one Entra app registration with "Allow public client flows" enabled and the delegated
`Mail.Read` permission. The confidentiality levels, the audit log and the privacy contract are
unchanged, and grants resolve to you because the tier performs no directory lookups. See
[docs/personal-profile.md](docs/personal-profile.md).

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

**How packaging works:** per-daemon env files at `~/.threadweave/daemons/<name>.env` hold secrets and options (one place, not shell history); `daemon run <name>` loads the env and dispatches. **Windows:** a launcher `.cmd` is dropped into the Startup folder (no admin needed) with logs to `~/.threadweave/logs/`. **Linux:** systemd units with `Restart=always`. Daemon env options: `THREADWEAVE_DAEMON_INTERVAL`, `THREADWEAVE_EMAIL_MAILBOX`, `THREADWEAVE_SP_SITE`, `THREADWEAVE_SP_ONENOTE`, `PORT` (bot).

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

- [M365 Connector Setup](docs/m365-connectors.md) — Azure app registrations, email/SharePoint/OneNote daemons, troubleshooting
- [Privacy Model](docs/privacy.md) — on-prem one-way contract, opt-out, right to delete, access control
- [Personal Profile](docs/personal-profile.md) — the single-user deployment: one mailbox, one owner, delegated access, no client secret and no admin grants
- [Distribution](docs/distribution.md) — how orgs get the app (manual upload, scripted publish, Teams Store), verified marketplace costs
- [Enterprise Adoption Checklist](docs/enterprise-adoption.md) — tracked gates from the IT-manager review: permissions, licensing, vendor readiness, observability, data lifecycle
- [Typed Decision Layer](docs/decision-layer.md) — the ingest judgments asked as typed questions, the three on-prem provider backends (local NLI encoder, Laya, ollama), phrasing rules, calibration, and the measurements behind them
- [Pre-push Gate](docs/pre-push-gate.md) — local hygiene hook before publishing: secrets, identifiers, files that must not leave
- [Technical Specification](docs/technical-spec.md)

## Release process

Releases are fully automatic. The GitHub Action
(`.github/workflows/auto-tag-release.yml`) runs on every push to master:
when code changed since the latest tag, it auto-increments the patch
version (0.4.0 → 0.4.1), tags it, and publishes the GitHub Release with
generated notes. Manual minor/major bumps (0.4.x → 0.5.0) are honored
as-is. Docs-only pushes produce no release. Nothing to approve, nothing
to remember.

The suite guards against drift: `tests/test_version_consistency.py` fails
if `pyproject.toml`, the API health version, and the changelog section
disagree, or if the version drifted without a tag or an in-flight bump.

To cut a manual minor/major release: bump `version` in `pyproject.toml`,
add a `## [x.y.z]` section to `CHANGELOG.md`, push. Patch releases
happen by themselves on every code push.

## Configuration

| Env Variable | Description |
|---|---|
| `MEMPALACE_PALACE_PATH` | MemPalace data directory (default: `~/.mempalace/palace/default`) |
| `THREADWEAVE_LLM_API_KEY` | Key for the LLM endpoint, if it needs one (not what enables detection) |
| `THREADWEAVE_LLM_BASE_URL` | Endpoint you run yourself; setting it is what enables LLM detection, otherwise the detector stays on regex. Ollama, vLLM, LiteLLM, llama.cpp server. Nothing reads `OPENAI_API_KEY` or `OPENAI_BASE_URL` |
| `THREADWEAVE_LLM_MODEL` | Model name (default: llama3.1:8b) |
| `THREADWEAVE_REQUIRE_AUTH` | Set to `1` to enable API key auth |
| `THREADWEAVE_API_KEYS` | `tenant:key,tenant:key` format. For roles/identity (admin, hr_admin, legal, wing, person_id) use `~/.threadweave/keys.json` instead |
| `THREADWEAVE_CORS_ORIGINS` | Comma-separated allowed origins (default: `*`). Restrict when exposed beyond local dev |
| `THREADWEAVE_AUDIT_DB` | Audit log database path (default: `~/.threadweave/audit.sqlite3`). Falls back to in-memory if the DB can't be opened |
| `THREADWEAVE_ENTRY_DB` | Entry store database URL. SQLite (default `sqlite:///~/.threadweave/entries.sqlite3`) works out of the box; PostgreSQL (`postgresql://user:***@host/db`, install `.[postgres]`) for corporate deployments. Entries survive API restarts |

**Connector extras:** `pip install -e ".[gws]"` (Google Workspace), `".[graph]"` (Graph reader flows), `".[teams]"`, `".[sharepoint]"`, `".[email]"`, `".[outlook]"`, or `".[all-connectors]"` for everything.

## Hardware recommendations

ThreadWeave runs fully on-prem. The CPU/GPU you need depends on whether you enable the
local LLM, and for what. Everything runs on CPU only; a GPU is only needed for faster
and higher-quality LLM detection and translation.

### Minimum (CPU only, no LLM)

- 4 vCPU / 8 GB RAM
- 20 GB disk
- Runs detection on the built-in regex engine (English-first) and hybrid search.
- No GPU required.

### Recommended (LLM detection + all-language translation)

- 8 vCPU / 16 GB RAM
- **NVIDIA GPU with 12 GB VRAM** (e.g. RTX 3060 Ti / 4060 Ti / 4070)
- 30 GB disk
- Runs a local LLM (Ollama) for multilingual detection and translation.

### Model tiers (Ollama)

| Scope | Model | VRAM | Notes |
|---|---|---|---|
| Testing / baseline | `qwen3.5:9b` | ~6.6 GB | Already pulled by default; good for building and testing the pipeline |
| All-language production | `qwen3:14b` | ~10 GB (Q4) | Qwen3 supports 100+ languages natively; recommended for serving all world languages |

A 12 GB GPU runs `qwen3:14b` (Q4, ~10 GB) comfortably. For 7B-class models (light
multilingual) an 8 GB GPU suffices. A GPU with 24 GB unlocks 30B-class models
(e.g. `qwen3-coder:30b`) if you later want higher translation fidelity.

Translation and multilingual detection stay local — no data ever leaves the on-prem host.

## What's Built

- ✅ **Detection engine** — Regex + LLM two-tier classifier (ANSWER/DECISION/QUESTION/CHAT/REFERENCE)
- ✅ **Typed decision layer** — the ingest judgments (worth saving, gossip, PII, language, scope) asked as typed questions and answered with probability distributions instead of parsed prose. One provider interface, three backends, all of them on the machine the content was captured on: a local NLI encoder (`bge-m3-zeroshot-v2.0-c`, distributions from logits, no GPU needed), the `laya` non-autoregressive decision model, and ollama for comparison. Thresholds and escalation live in code, per-question affine calibration on logits, and every evaluation is written to an audit log. Off by default. See [docs/decision-layer.md](docs/decision-layer.md)
- ✅ **PII detection** — International regex patterns (EN/NO/DE/FR/ES/IT) + LLM prompt hardening. Catches SSN, credit cards, IBAN, bank accounts, passport numbers, salary figures, home addresses, and medical data without false-flagging company names or workplace identifiers.
- ✅ **Ingestion pipeline** — Central dedup → detect → PII gate → store
- ✅ **MemPalace integration** — Hybrid search (BM25 + vector cosine)
- ✅ **Org model** — Full MemPalace Knowledge Graph integration with temporal triples. Team membership, reporting chains, relevant-people search, HRIS bulk sync. Dual-mode: with or without KG.
- ✅ **Hallway/Tunnel graph navigation** — D3.js force-directed graph visualization in the web dashboard. Click-to-highlight, drag-to-rearrange, wing filter, hallway (within-wing) vs tunnel (cross-wing) edge coloring. **Knowledge entries are nodes too**: captured decisions/answers render as violet diamonds linked to their author (authored_by) and wing (belongs_to).
- ✅ **Web dashboard** — Single-file SPA with pipeline overview, search, save, entries, and graph tabs.
- ✅ **API server** — FastAPI with auto-generated docs
- ✅ **CLI** — `detect`, `search`, `save`, `serve`
- ✅ **Confidentiality** — 7 sensitivity levels with access enforcement + audit
- ✅ **Google Workspace connector** — Gmail, Chat, Drive ingestion + offboarding harvester
- ✅ **Profiling** — Latency percentiles, throughput, Prometheus export
- ✅ **Auth** — Opt-in API key middleware with tenant scoping
- ✅ **Docker** — Multi-stage build with optional Ollama profile
- ✅ **Teams bot connector** — @mention capture + passive detection with consent card, RSC group-chat capture, privacy commands (`opt out`, `opt in`, `delete <topic>`, `status`), RSC consent probe (warns when admin consent is missing instead of silently going @mention-only)
- ✅ **Teams watch daemon** — Graph delta polling of channel messages with app-only permissions. Captures every channel in every team with no bot installs, no @mentions, no RSC consent. Prime mode starts from install time; `--backfill` mines channel history. Delta tokens persist across restarts.
- ✅ **Email watch daemon** — continuous one-way mailbox polling, thread-aware capture, sender→department→wing mapping
- ✅ **SharePoint watch daemon** — delta-polling of document libraries (new + edited files), xlsx/pptx/docx/pdf extraction, OneNote notebook polling via delegated auth
- ✅ **Privacy layer** — opt-out registry (ingest gate + early daemon skips), audited right-to-delete, Teams privacy commands
- ✅ **Capture notifications** — daemons queue a camera-sign notice for the content author; delivery is a personal Teams DM for authors the bot knows, a Teams activity-feed notification via Graph (`TeamsActivity.Send`) for everyone captured passively, and an email fallback via Graph `sendMail` (`Mail.Send`) when the tenant refuses activity notifications. Undeliverable notices are marked skipped after retries, never silently dropped.
- ✅ **Durable entry store** — SQLAlchemy persistence, SQLite default (`~/.threadweave/entries.sqlite3`) or PostgreSQL via `THREADWEAVE_ENTRY_DB`; the palace survives restarts
- ✅ **Entry versioning** — re-captured documents (same source_file) chain to the original; `GET /api/v1/entries/{id}/versions` shows the evolution
- ✅ **Daemon packaging** — `threadweave daemon install|status|run`: Windows Startup launchers, systemd units, per-daemon env files
- ✅ **Teams app distribution** — deterministic package builder + scripted org-catalog publish (`threadweave teams package|publish`)
- ✅ **OpenDocument support** — odt/ods/odp (LibreOffice native) extracted with stdlib only
- ✅ **Visio + video/audio** — .vsdx diagram text extraction; on-prem video/audio transcription (ffmpeg + faster-whisper, CPU)
- ✅ **Personal profile** — the same installation as a single-user deployment: one mailbox, one owner, delegated device-code mailbox access with no client secret and no application permission, captures filed under the `personal` tenant, and no RSC or admin grants. See [docs/personal-profile.md](docs/personal-profile.md)
- ✅ **925 tests** — full suite green (11 skipped)

## What's Next

- [ ] Detector tuning with real capture noise (the 0.25 short-decision gap)
