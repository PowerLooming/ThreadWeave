# Data handling

What ThreadWeave does with data, written so a procurement, IT or data-protection
conversation can cite it. Every statement here maps to shipped code, and the
paths named are the evidence. This page is not a contract and it is not legal
advice.

## The short version

- ThreadWeave is software you install and run. The project operates no service,
  receives no data and holds no copy of your captures. There is no telemetry, no
  licence check, no crash reporting and no update ping.
- Content is read from the systems you point the connectors at, and stored on
  the host you choose. The capture and storage path has no destination it could
  send content to.
- There are no subprocessors. The third parties in the data path are the ones
  you already contract with (Microsoft 365, Google Workspace or an IMAP
  provider), plus any model endpoint you configure yourself.

## Roles

- The customer is the controller and operates the instance. ThreadWeave runs on
  infrastructure the customer controls and writes only to the paths listed
  below.
- The maintainer is not a processor. No capture, entry, identifier or audit row
  is transmitted to the project or to Harald Daltveit, so there is no
  processing agreement to sign with him. Discussions and issues are the
  customer's choice, and captured content should never be pasted into them.
- A peer key is optional and off unless the customer issues one. It lets a
  named partner organisation search one wing over a mutual-TLS link, and every
  read is audited with that partner's name. The partner is the customer's
  choice, so any agreement behind it is the customer's to make. See
  [peer-keys.md](peer-keys.md).
- A configured model endpoint is the customer's choice too. Detection runs
  locally by default: with no `THREADWEAVE_LLM_BASE_URL` set, the detector is
  the built-in regex engine and no text leaves the host. If the customer points
  the detector at a hosted service, that provider becomes the customer's
  processor, chosen and contracted by them. Nothing reads `OPENAI_API_KEY` or
  `OPENAI_BASE_URL`, and there is no vendor endpoint default, so an environment
  carrying a cloud credential stays on regex (`src/threadweave/llm_detector.py`).
  The typed decision layer ships local backends only (`encoder`, `laya`,
  `ollama`), with no hosted backend (`src/threadweave/decision_providers.py`).

## What comes in

Every connector pulls. Nothing is pushed at ThreadWeave from the source system.

| Connector | Source and interface | What is read |
|---|---|---|
| `email watch` | Exchange Online through Microsoft Graph, app-only or delegated | new and sent mail: subject, body, sender and thread participants |
| `teams watch` | Microsoft Graph delta queries on channel messages | channel messages in every team the app registration can see |
| Teams bot | Bot Framework, conversations the bot is installed in or mentioned in | the message that triggered it |
| `sharepoint watch` | Microsoft Graph on document libraries, including OneNote | new and edited files, extracted to text |
| `org-sync` | Microsoft Graph (`Team.ReadBasic.All`, `TeamMember.Read.All`) | team names and membership, no content |
| Google Workspace connectors | Gmail, Chat and Drive APIs with a service account | mail, chat messages and documents |
| Harvester scripts | IMAP, exported `.eml`, classic Outlook via COM, or Graph device-code sign-in | mail |
| `POST /api/v1/ingest` | your own calls | whatever you send |

Content that the detector scores as knowledge becomes an entry. Everything else
is counted and dropped. Identifiers the PII gate detects are replaced with typed
placeholders before the entry is written, so the unredacted text is not stored
(`src/threadweave/text_hygiene.py`, [pii-gate-redaction.md](pii-gate-redaction.md)).

## What is stored, and where

| Store | Default path | Contents |
|---|---|---|
| Entry store | `~/.threadweave/entries.sqlite3`, or PostgreSQL via `THREADWEAVE_ENTRY_DB` | the captured entries with provenance, sensitivity, ACL and version chain |
| Palace index | `~/.mempalace/palace/default`, or `MEMPALACE_PALACE_PATH` | the same entries as MemPalace drawers, with the vector and keyword index |
| Audit log | `~/.threadweave/audit.sqlite3` | reads of confidential entries, refusals, deletions, ACL revocations. Entry id, wing, sensitivity, reason and a hashed IP, never the content |
| Opt-out registry | `~/.threadweave/optout.json` | the identities that asked not to be captured |
| Notification queue | `~/.threadweave/notifications.sqlite3` | capture notices waiting for delivery |
| Daemon state | `~/.threadweave/` | delta tokens, OneNote watermarks, MSAL token cache, bot conversation references, `keys.json`, `daemons/*.env`. Credentials and resumption state, no captured content |
| Logs | `~/.threadweave/logs/<name>.log` | poll results, message and thread ids, error text. Daemons do not log message bodies |
| Decision audit | `~/.threadweave/decision_audit.jsonl` | one row per typed decision when that layer is enabled |

The SQLite stores are ordinary files, so file permissions, disk encryption and
backup policy are the operator's. PostgreSQL is the option for a deployment
where the database is already operated that way.

## What leaves the host

| Destination | Direction and trigger | Content |
|---|---|---|
| Microsoft Graph | outbound request on every poll and every write to a source system | query parameters and ids in the request, source content in the response |
| Google Workspace APIs | outbound request on every poll | same pattern |
| Capture notification | to the entry's author, on each capture | the entry title, wing and room and a link to the source message, never the body |
| Teams bot reply | to the person in the conversation they used | their own answer, cards and privacy commands |
| Configured model endpoint | on each classification, when one is configured | the text being classified |
| MCP server | an inbound listener for agents on your network, default `http://127.0.0.1:8100/mcp` | answers the caller's query with that caller's clearance, and audits the read |
| Peer key read | an inbound request from a partner's agent you authorised | one wing, capped results, audited |
| Teams app package publish | when an administrator runs `threadweave teams publish` | the app package, no captured content |

No captured content goes to the maintainer, to GitHub, or to any service the
project operates. The Graph connector that could push entries into a tenant
search index was removed, and `tests/test_daemons.py::test_the_copilot_graph_daemon_is_gone`
pins its absence. The full position, including the reader-facing surfaces and
their audit rules, is in [ai-publication-boundary.md](ai-publication-boundary.md).

Authentication is opt-in. `THREADWEAVE_REQUIRE_AUTH` turns it on and
`THREADWEAVE_API_KEYS` or `~/.threadweave/keys.json` supplies the keys with
their roles and tenant scope. An instance reached by anything other than the
local host needs authentication enabled, and the API bound to the interface the
operator intends.

## Subprocessors

None. ThreadWeave is software with no hosted component: no account, no
activation, no analytics, no crash upload, no update check and no hosted API.
The maintainer has no technical means to reach a customer instance and does not
ask for one.

For a procurement record, the parties to list are the customer's own:

- Microsoft 365 and Exchange Online, already under the customer's Microsoft
  agreement, for the tenants the connectors read
- Google Workspace, where the Google connectors are used, already under the
  customer's Google agreement
- the IMAP provider, where `ingest_imap.py` is used
- the hosting provider, if the instance runs on rented infrastructure
- any model endpoint the customer configures, if it is not on their own host

The project's website is a static site and its email address is a mailbox.
Nothing in the product sends data to either.

## Retention

The defaults in shipped code, stated plainly because a data protection officer
will ask:

- Entries are kept until they are deleted. There is no automatic expiry and no
  per-tenant or per-source retention setting. A scheduled purge is not shipped
  yet.
- The audit log is kept indefinitely. Its rows are content-free.
- Notification rows stay until delivered, or until they are marked skipped
  after `THREADWEAVE_NOTIFY_MAX_ATTEMPTS`.
- Logs grow until the operator rotates them. No rotation is shipped.
- Daemon state stays until the operator removes it.

Because no purge is shipped, the accurate statement for a data processing
agreement is that retention is indefinite until an operator deletes. Do not sign
a schedule that assumes ThreadWeave enforces one. A retention policy today is
operational: delete on request through the API, or run a scheduled delete
against the entry store and the palace.

## Deletion

Deletes are explicit, authorised and audited.

- Routes: `DELETE /api/v1/entries/{id}`, and the Teams command
  `delete <topic>`, which removes that author's entries matching a topic.
  There is no CLI verb for deleting entries and no soft delete.
- Rights: the entry's author, a same-wing member for public and internal
  entries, or a key with the `admin`, `legal` or `hr_admin` role. A refused
  attempt is audited too.
- What a delete removes: the entry from the in-memory store, the entry store and
  the palace index, so it stops appearing in search results. The audit row keeps
  the entry id, wing and sensitivity, and never the content
  (`src/threadweave/api.py`, the delete handler).
- Permanence: there is no recycle bin and no undo. A backup taken before the
  deletion still contains the entry, and a restore brings it back, so backup
  retention is part of a deletion policy.
- Versions: a re-captured document is stored as a new version chained to the
  earlier entry. Deleting one version deletes that entry only;
  `GET /api/v1/entries/{id}/versions` lists the chain so the rest can be deleted
  too.
- Re-capture: deleting an entry does not stop the same source being captured
  again if a later poll still scores it as knowledge. Opt-out is the control
  that stops future capture for a person, and it is checked at ingest and in
  every daemon before extraction.
- Bulk deletion for a legal or supervisory request is not a shipped workflow.
  The Teams `delete <topic>` command is the only bulk path, and it removes one
  recorded entry at a time.

## Security measures, briefly

- Access is decided per entry from seven sensitivity levels, wing membership,
  client assignment, a named person list and source ACLs, and every gate fails
  closed. See [privacy.md](privacy.md).
- Reads of confidential entries and above, refusals and deletions are audited.
- Identifiers are redacted at ingest rather than the message being dropped.
- Traffic to Microsoft and Google is HTTPS through their official SDKs and
  clients. Credentials sit in per-daemon environment files under
  `~/.threadweave/daemons/` rather than in shell history.
- The security policy, the disclosure route and the scope are in
  [SECURITY.md](../SECURITY.md).

There is no certification to point at. The project holds no SOC 2, ISO 27001 or
similar attestation and will not claim one. If your process requires an
attestation before a pilot, that is a real blocker and the honest answer is that
it is not available today.

## Questions

Open a Thread in [Discussions](https://github.com/PowerLooming/ThreadWeave/discussions)
for anything this page does not answer, and see [support.md](support.md) for
response expectations. For a vulnerability, follow [SECURITY.md](../SECURITY.md)
rather than posting publicly.
