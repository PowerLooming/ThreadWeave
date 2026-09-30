# The AI publication boundary

Status: design. Nothing implemented here yet. Written because the shipped Copilot/Graph
connector contradicts `docs/privacy.md` ("It never leaves to a third party, and it never
comes back") by pushing captured content into the M365 Copilot index from inside the capture
path, unsupervised, every five minutes.

## The principle

AI is part of how work gets done, so ThreadWeave needs an interface for it. What it must not
have is an interface that shares by accident. Storing knowledge and sharing knowledge are two
different capabilities, and the code has to keep them apart so that nobody, and no service,
can move content outward as a side effect of normal operation.

Stated as one line for the contract: **storage never exports; publication is a separate,
deliberate act, enumerated in the contract.**

## What exists today

Three surfaces can move content out of the on-prem host. They are not equal.

| surface | direction | who initiates | identity | filtered | audited |
|---|---|---|---|---|---|
| MCP server (`mcp_server.py`) | pull | the agent, per query | the caller's own API key | yes, confidentiality-filtered for that key | yes, like a person's read |
| Copilot/Graph connector (`connectors/graph`) | **push** | a daemon, every 300 s | a daemon key, `role=readwrite`, no claims | it *fails* on anything the key cannot read, then pushes an empty stub | only the denials |
| Capture notification (`notify.py`) | push | a daemon, per capture | service | title/topic only, never the body | not recorded as a publication |

The MCP server is the model to follow: explicit, per-caller, filtered and audited. The other
two are accidents waiting to be found: the connector silently exported 29 entries and pushed
five of them as empty documents, and it reports `0 failed (100.0% success rate)` while upsert
calls are returning 502/503/504.

## Design: two layers that cannot be confused

**Storage layer.** Ingest, detection, screening, MemPalace, the entry store, the search API.
It may read from M365 and must have no capability to send content anywhere. That is a
property to enforce, not to document: see the enforcement section.

**Publication layer.** The only code in the repository allowed to send content outward, in
one package, `src/threadweave/publish/`:

- a destination is named at call time and never defaulted. No destination configured means
  publishing is impossible, and configuring one does not enable anything by itself.
- a run is explicit: a CLI command or an API call, with a preview first. The preview reports
  exactly what would leave, item ids and counts and destination, before anything is sent;
  sending requires a second, explicit flag.
- the same gate applies as to a read: the requester's clearance is checked per entry, the
  PII redactor runs on the exported copy, and the opt-out registry is respected. An exported
  item is filtered exactly as it would be for the person exporting it.
- every publication is audited as content-free metadata: requester, destination, item ids,
  counts, timestamp. The audit answers "what left, to where, on whose authority".

## Rules that make it mechanical

R1. **No scheduled publisher.** Publishing is never a daemon, a watch, or a cron job that
sends. A schedule may only *ask*: it notifies a human that a publication is pending.

R2. **Fail closed without a named destination.** An implementation that cannot determine
where content would go must refuse to send, not pick a default.

R3. **Preview before send.** A publication without a preview is a bug, including in tests.

R4. **One package.** All outbound-with-content code lives in `publish/`. Nothing else may
import an outbound client or contain a destination endpoint. This is test-enforced.

R5. **Filtered on the way out.** Clearance, PII redaction and opt-out apply at publication
time, using the same code as the read path, so the two can never drift.

R6. **Audited, content-free.** A publication that leaves no audit record is a bug.

R7. **Enumerated in the contract.** `docs/privacy.md` names every surface that can carry
content outward, and the one-way sentence is scoped to storage.

## Enforcement: how "not accidental" becomes checkable

Documentation did not prevent the current situation, so the boundary needs tests that fail
when it is crossed:

- an import-graph test: no module outside `publish/` may import the outbound client, and
  `publish/` may be imported only by the CLI and the API endpoint that expose it;
- a source scan: destination endpoints (`external/connections`, `graph.microsoft.com`
  upserts, any external AI service URL) may appear only under `publish/`;
- a test that `daemon install` refuses to install any publisher, with the daemon list
  asserted to contain none;
- a test that a publication with no destination fails closed;
- a test that a publication applies clearance, redaction and opt-out, by exporting the same
  entry as two identities and asserting different output;
- a test that the audit record exists after a publication and contains no content.

## Decision taken: the Copilot destination is not shipped

Harald's call, and the reasoning is testability plus the promise: he holds no Copilot licence
in the tenant, so this connector is the one surface he cannot test without paying for one or
waiting for the KM pilot, and an untested component that breaks the on-prem promise is the
worst of both. **The Copilot/Graph connector is removed.** No destination ships in the public
repository for now, and there is no publisher in the daemon registry.

Removed: `src/threadweave/connectors/graph/` (auth, connector, schema, sync), the
`graph setup|sync|status|daemon` CLI verbs, the `graph-daemon` daemon entry, and the
connector's tests. The empty-stub fallback disappears with it; if a destination is ever
added, an entry the caller cannot read is skipped and counted, and a document with empty
content is never sent to a search index.

Kept: the Graph **pull** clients the email, SharePoint and Teams watchers need, the MCP
server (the pull interface, already obeying R5 and R6), and the deterministic gate.

What is left to build when a destination is wanted: the `publish/` package with destination
resolution, preview, clearance, redaction and audit, invoked by an operator. CLI first is the
natural shape, since R1 to R6 are all about an operator deciding.


## Smallest useful first slice

If a destination is ever wanted: the `publish/` package with its core (destination resolution,
preview, clearance, redaction, audit), the CLI verb, the enforcement tests, and one adapter
for that destination. No daemon, no scheduling, no API endpoint until the CLI shape has
proved itself.
