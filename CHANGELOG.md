# Changelog

All notable changes to ThreadWeave are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
