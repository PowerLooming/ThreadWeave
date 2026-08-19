# Changelog

All notable changes to ThreadWeave are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.6] — 2026-08-19



## [0.4.5] — 2026-08-19



## [0.4.4] — 2026-08-17

### Added

- **Org tracker** (`threadweave org sync`, `org-sync` daemon): polls teams and members from Graph and reconciles temporal person→team edges in the org model. Members who leave get their edges closed, so "who is in which team" stays current and historical. Feeds the dashboard Graph page and `/api/v1/org/people/{id}/team`. Requires `TeamMember.Read.All` (application) on the Graph app registration.

## [0.4.3] — 2026-08-17



## [0.4.2] — 2026-08-17

## [0.4.1] — 2026-08-17

### Added

- **Teams watch daemon** (`threadweave teams watch`): continuous one-way harvesting of Teams channel messages via Graph delta polling with app-only permissions (`ChannelMessage.Read.All` + `Team.ReadBasic.All`). Captures every channel in every team with no bot installs, no @mentions, no RSC consent. Prime mode (default) starts capturing from install time; `--backfill` processes channel history with a per-channel cap. Delta tokens persist to `~/.threadweave/teams_delta.json`. Filters system events, bot posts, unattributable messages, opted-out authors, and sub-50-character texts before the central ingest pipeline. Registered as the `teams-watch` daemon.
- **Activity-feed capture notifications**: the camera sign now reaches passively captured authors. Delivery chain per notification: personal DM when the author has a 1:1 conversation with the bot, then a Teams activity-feed notification via Graph (`TeamsActivity.Send`, `POST /users/{id}/teamwork/sendActivityNotification`), then an email fallback via Graph `sendMail` (`Mail.Send`, `THREADWEAVE_NOTIFY_SENDER`) for tenants that refuse `TeamsActivity.Send`. Channel/group-chat conversation refs are never used for capture DMs (fixes public-channel posting). Undeliverable notifications are marked skipped after `THREADWEAVE_NOTIFY_MAX_ATTEMPTS` retries (default 5) and surfaced in `/api/v1/notifications/stats`.
- **RSC consent probe**: declaring RSC in the manifest is not consent. The bot now tracks the teams it observes and verifies each team's granted RSC permissions via Graph `GET /teams/{id}/permissionGrants` at startup and on every new team. Missing consent logs a loud warning instead of silently degrading to @mention-only capture; results are exposed as `rsc_status` on the bot's `/health` endpoint. Requires `TeamsAppInstallation.ReadForTeam.All` for the verification call.

## [0.4.0] — 2026-08-07

### Added

- **Email watch daemon** (`threadweave email watch`) — continuous one-way M365 → on-prem polling. Thread-aware capture (conversationId grouping), in-memory dedup, `--mark-read` opt-in, resilient to Graph outages.
- **SharePoint watch daemon** (`threadweave sharepoint watch`) — continuous delta-polling of document libraries. Delta tokens persist to `~/.threadweave/sharepoint_delta.json` so restarts resume instead of re-crawling. Catches new files **and edits** (content-hash based). Site filter, per-site error containment (known 403s don't stop the loop).
- **OneNote support** (`sharepoint watch --onenote` + `sharepoint onenote-login`) — reads notebooks via the Graph OneNote API with **delegated** auth (Microsoft deprecated app-only tokens for OneNote on 2025-03-31). Device-code sign-in, persisted MSAL cache, watermark-based polling (no delta API exists; new pages and edits caught).
- **xlsx + pptx extraction** — openpyxl and python-pptx backed (previously listed in `SUPPORTED_EXTENSIONS` but extracted to empty text and silently skipped). Sheet names and speaker notes preserved as context.
- **OpenDocument extraction** — `odt`/`ods`/`odp` (LibreOffice native) via stdlib only: paragraphs, headings, and spreadsheet cells. Linux orgs using LibreOffice are first-class; no optional dependency needed.
- **Visio + video/audio** — `.vsdx` diagrams (shape text, stdlib) and on-prem transcription of videos/audio via ffmpeg + faster-whisper (CPU, cached model, `THREADWEAVE_WHISPER_MODEL`); audio never leaves the host. Live-verified: TTS video → transcript → decision captured.
- **Privacy layer** — the "camera sign":
  - `OptOutStore` (`~/.threadweave/optout.json`) with API endpoints `GET /api/v1/optout`, `POST /api/v1/optout/out`, `POST /api/v1/optout/in`
  - Opt-out gate at ingest step 0 (checks author_id / email_sender / email_participants / participants) and early skips in the email/SharePoint daemons (content never extracted)
  - `DELETE /api/v1/entries/{id}` with author / same-wing / admin rights; every deletion audited (`AuditLog.log_delete`)
  - Teams bot privacy commands: `opt out`, `opt in`, `delete <topic>`, `status`
  - Search results now carry `author_id`
- **Teams bot wing/room mapping** — wing derived from conversation context (team name → wing, channel → room), matching the email department mapping.
- **Email palace wing mapping** — sender → department → wing via Graph `User.Read.All`, recipient fallback, `email` fallback wing.
- **Documentation** — `docs/privacy.md` (privacy model), expanded `docs/m365-connectors.md` (daemons, OneNote, delegated auth, troubleshooting), README continuous-capture + privacy sections.
- **Capture notifications** — when daemons save knowledge from someone's content, the Teams bot DMs that person ("camera sign" that talks): durable SQLite queue, author-only, email→AAD resolution, proactive delivery via `continue_conversation`, opt-out respected. Live-verified 2026-08-08.
- **Entry versioning** — re-captured documents (same `source_file`) chain to the original via a `version_of` pointer instead of piling up unrelated duplicates; `GET /api/v1/entries/{id}/versions` returns the evolution chain. Schema migration adds `version_of` to existing DBs. Live-verified 2026-08-08.
- **Daemon packaging** — `threadweave daemon run|install|uninstall|status|config`: per-daemon env files (`~/.threadweave/daemons/<name>.env`), Windows Startup-folder launchers (no admin), systemd units with `Restart=always`, in-process dispatch (execvpe segfaults on MSYS Windows). Live-verified: all four launchers installed and polling.
- **Durable entry store** — SQLAlchemy-based persistence. SQLite default (`~/.threadweave/entries.sqlite3`) for zero-config on-prem; PostgreSQL via `THREADWEAVE_ENTRY_DB` URL + `.[postgres]` extra for corporate deployments. The palace survives restarts on either backend; startup reloads persisted entries into the memory stores. Verified live: save → restart → entry restored and searchable.

### Fixed

- **SharePoint download crash** — Graph returns a 302 to `download.aspx`; httpx now follows redirects (`follow_redirects=True`). Every download previously failed.
- **SharePoint folder recursion** — `process_drive` skipped all folders ("recursion can be added" comment), so folder-organized libraries imported zero documents. Now recursive with a depth bound; folder detection uses key-presence (`"folder" in item` — empty `{}` folder objects are falsy).
- **SharePoint delta URL doubling** — Graph delta links are full URLs; passing them through `_request` (which prepends the base) doubled the host → 404 on every resume. Absolute URLs now use `_request_url`.
- **Email wing recipient fallback** — `process_message` didn't pass participants, so department fallback never saw recipients.
- **Wing cache poisoning** — cached "email" (unknown user) short-circuited recipient fallback; cache hits only satisfy when they hold a real wing.
- **"promotion" HR false positive** — retail promo content (e.g. "seasonal promotion review") was classified `hr_privileged` and locked out of its own wing; "promotion" now requires career context (demotion/reorganization/org chart stay unconditional).

### Changed

- Full test suite: **393 passed** (was 312 at 0.3.0). The live-PostgreSQL roundtrip test is gated on `TEST_POSTGRES_URL`.
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
