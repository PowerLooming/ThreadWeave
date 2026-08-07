# Changelog

All notable changes to ThreadWeave are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-08-07

### Added

- **Email watch daemon** (`threadweave email watch`) — continuous one-way M365 → on-prem polling. Thread-aware capture (conversationId grouping), in-memory dedup, `--mark-read` opt-in, resilient to Graph outages.
- **SharePoint watch daemon** (`threadweave sharepoint watch`) — continuous delta-polling of document libraries. Delta tokens persist to `~/.threadweave/sharepoint_delta.json` so restarts resume instead of re-crawling. Catches new files **and edits** (content-hash based). Site filter, per-site error containment (known 403s don't stop the loop).
- **OneNote support** (`sharepoint watch --onenote` + `sharepoint onenote-login`) — reads notebooks via the Graph OneNote API with **delegated** auth (Microsoft deprecated app-only tokens for OneNote on 2025-03-31). Device-code sign-in, persisted MSAL cache, watermark-based polling (no delta API exists; new pages and edits caught).
- **xlsx + pptx extraction** — openpyxl and python-pptx backed (previously listed in `SUPPORTED_EXTENSIONS` but extracted to empty text and silently skipped). Sheet names and speaker notes preserved as context.
- **Privacy layer** — the "camera sign":
  - `OptOutStore` (`~/.threadweave/optout.json`) with API endpoints `GET /api/v1/optout`, `POST /api/v1/optout/out`, `POST /api/v1/optout/in`
  - Opt-out gate at ingest step 0 (checks author_id / email_sender / email_participants / participants) and early skips in the email/SharePoint daemons (content never extracted)
  - `DELETE /api/v1/entries/{id}` with author / same-wing / admin rights; every deletion audited (`AuditLog.log_delete`)
  - Teams bot privacy commands: `opt out`, `opt in`, `delete <topic>`, `status`
  - Search results now carry `author_id`
- **Teams bot wing/room mapping** — wing derived from conversation context (team name → wing, channel → room), matching the email department mapping.
- **Email palace wing mapping** — sender → department → wing via Graph `User.Read.All`, recipient fallback, `email` fallback wing.
- **Documentation** — `docs/privacy.md` (privacy model), expanded `docs/m365-connectors.md` (daemons, OneNote, delegated auth, troubleshooting), README continuous-capture + privacy sections.
- **Durable entry store** — SQLAlchemy-based persistence. SQLite default (`~/.threadweave/entries.sqlite3`) for zero-config on-prem; PostgreSQL via `THREADWEAVE_ENTRY_DB` URL + `.[postgres]` extra for corporate deployments. The palace survives restarts on either backend; startup reloads persisted entries into the memory stores. Verified live: save → restart → entry restored and searchable.

### Fixed

- **SharePoint download crash** — Graph returns a 302 to `download.aspx`; httpx now follows redirects (`follow_redirects=True`). Every download previously failed.
- **SharePoint folder recursion** — `process_drive` skipped all folders ("recursion can be added" comment), so folder-organized libraries imported zero documents. Now recursive with a depth bound; folder detection uses key-presence (`"folder" in item` — empty `{}` folder objects are falsy).
- **SharePoint delta URL doubling** — Graph delta links are full URLs; passing them through `_request` (which prepends the base) doubled the host → 404 on every resume. Absolute URLs now use `_request_url`.
- **Email wing recipient fallback** — `process_message` didn't pass participants, so department fallback never saw recipients.
- **Wing cache poisoning** — cached "email" (unknown user) short-circuited recipient fallback; cache hits only satisfy when they hold a real wing.
- **"promotion" HR false positive** — retail promo content (e.g. "seasonal promotion review") was classified `hr_privileged` and locked out of its own wing; "promotion" now requires career context (demotion/reorganization/org chart stay unconditional).

### Changed

- Full test suite: **367 passed, 1 skipped** (was 312 at 0.3.0). The skipped test is the live-PostgreSQL roundtrip, gated on `TEST_POSTGRES_URL`.
- `threadweave sharepoint watch` gained `--onenote` and `--site` flags; `sharepoint onenote-login` command added.
- GraphReader app registration now also needs `User.Read.All` (Application) and `Notes.Read.All` (Delegated) plus "Allow public client flows" enabled (see `docs/m365-connectors.md`).
- Entry storage is durable and backend-pluggable (SQLAlchemy: SQLite default, PostgreSQL via `THREADWEAVE_ENTRY_DB` URL); the in-memory store is reloaded from the DB at startup.

## [0.3.0] — 2026-08-01

### Fixed

- **Semantic search returned at most one result.** MemPalace's BM25
  re-ranking dropped drawer IDs, so every result came back with an empty id
  and the API's dedup collapsed all but the first hit. IDs are now embedded
  before re-ranking and survive it.
- **Search bypassed confidentiality and tenant isolation.** MemPalace
  results were hardcoded to `sensitivity: "internal"` and never
  tenant-filtered. Entries now store sensitivity and tenant metadata on
  write and carry it through search.
- **Tenant scoping was enforced on ingest only.** Search, entry retrieval,
  tenant listing, wings/rooms, and audit endpoints now scope to the API
  key's tenant. A tenant key can no longer read another tenant's data,
  including via body `tenant_id` claims.
- **Confidentiality ACLs failed open.** Empty requester wing, RESTRICTED
  entries without an `allowed_people` list, and CLIENT_CONFIDENTIAL entries
  without a `client_id` no longer bypass access control. Requester identity
  now comes from the API key (`keys.json` role/wing/person_id) when auth is
  enabled; unauthenticated body claims are ignored.
- **Dedup blocked retries of rejected content.** Content rejected for PII
  or below the save threshold stayed deduped for the server run. The dedup
  hash is now recorded only on save, and the dedup key includes the tenant
  so tenants no longer cross-dedupe.
- **Test suite is fully green** (312 passed). The four pre-existing
  failures asserted the old 0.15 save threshold; they now match the 0.40
  detector contract.

### Changed

- Entry IDs are full 128-bit UUIDs (were 8-char truncated).
- Audit entries record the tenant and a hashed client IP.
- CORS origins configurable via `THREADWEAVE_CORS_ORIGINS`.
- `requests` and `psutil` are base dependencies; new `gws` and `graph`
  connector extras declared.
- FastAPI startup uses lifespan instead of the deprecated `on_event`.
- The audit log is durable (SQLite at `~/.threadweave/audit.sqlite3`,
  override with `THREADWEAVE_AUDIT_DB`) instead of in-memory only.

## [0.2.0] — 2026-07-31

- Relicensed AGPL → MIT.
- International PII detection (EN/NO/DE/FR/ES/IT) with context gating.
- MemPalace knowledge graph org model integration (temporal triples,
  hallway/tunnel graph navigation, D3.js dashboard).
- Google Workspace connectors (Gmail, Chat, Drive) live-tested.
- SharePoint watcher live-tested.
- `docs/m365-connectors.md` setup guide.
