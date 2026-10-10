# ThreadWeave Privacy Model

ThreadWeave captures organizational knowledge so nobody has to ask the person who knows. Passive capture without disclosure is surveillance, so the privacy model is a first-class feature, not a footnote.

## The contract

**Content flows one way: from Microsoft 365 and Google Workspace into the store on the host you control. The capture and storage path never sends captured content out.**

- Every connector pulls. Content arrives over outbound Graph API polling (`email-watch`, `sharepoint-watch`, `teams-watch`), IMAP, classic Outlook COM, or an exported file, and none of those paths holds a destination to send content to.
- Two surfaces can carry anything outward, and both are named here:
  - The MCP server (`src/threadweave/mcp_server.py`), a pull interface for agents the customer runs on their own network. The caller uses its own API key, results are confidentiality-filtered for that key, and the read is audited like a person's read.
  - The capture notification, described under the camera sign below. It sends an entry's title and wing to its author, never the body.
- No cloud assistant route is open, and that is a decision, not a gap. A cloud assistant cannot reach a private endpoint without a broker, and exposing this server through one is what the product refuses. The rule, the reasoning and the tests that pin it are in [ai-publication-boundary.md](ai-publication-boundary.md).
- Processing stays on the host: detection, the PII gate, storage and the language model. The LLM detector reads only `THREADWEAVE_LLM_*` variables and has no vendor default endpoint, so an ambient cloud credential cannot point it at a service the customer does not run, and the typed decision layer ships local backends only.
- Removed: ThreadWeave used to ship a Microsoft 365 Copilot (Graph) connector that pushed captured entries into the tenant's search index. It contradicted this page, so it went, together with its CLI verbs and its daemon.

## The "camera sign": five layers

### 1. Transparency (what we watch, what we captured)

- Every entry records provenance: `source_type` for the connector it came from, `author_id`, `created_at`, `wing` and `room`, plus the connector's own fields in `source_metadata` (`source_file`, `message_url`, thread participants).
- The audit log (`GET /api/v1/audit/recent`, SQLite at `~/.threadweave/audit.sqlite3`) records a direct read of an entry (`action=view`), a refused direct access, a search result the requester could not see, an ACL revocation, and every deletion (`action=delete`). Repeated identical events (the same requester retrying a denial) fold into one row carrying `count`, `first_seen` and the last event's `timestamp`, so a runaway client cannot bury the distinct events behind a wall of repeats; the endpoint reports both `total` rows and `events` recorded.
- The dashboard (`src/threadweave/static/index.html`) shows the pipeline as it runs: the ingested, saved, skipped and PII-rejected counters from `GET /api/v1/metrics`, per-stage latency, the detection engine mix, throughput, memory, and the recent entries list.

### 2. Consent at capture (Teams)

- The Teams bot never stores anything silently. It detects knowledge, offers an Adaptive Card, and saves only when the author presses Save. `THREADWEAVE_BOT_MODE` chooses where it listens: `both`, the default, prompts on any message in a conversation it can see and on an explicit request, while `explicit` reacts to @mentions only.
- Teams channels are also captured automatically by the pull-only `teams-watch` daemon, and email, SharePoint and OneNote are automatic by design (there is no human at capture time). That is why layers 3, 4 and 5 exist.

### 3. Capture notification (the sign that talks)

When a daemon saves knowledge from someone's content, that person gets a notice from the bot:

> Captured to the palace. Your Teams message "<title>" was added (wing: <wing>, room: <room>). Reply delete <title> to remove it, or opt out to stop future captures.

- Notifications are queued only for the content author (`~/.threadweave/notifications.sqlite3`), never for unrelated people.
- Opted-out authors never generate entries, so they never generate notifications.
- Delivery has three legs, tried in order: a Teams direct message when the author has a stored one-to-one conversation reference, a Teams activity feed notification, and email. Channel and group chat references are never used for a capture notice, because that would post it publicly.
- The bot polls `GET /api/v1/notifications/pending` every 60s (configurable with `THREADWEAVE_NOTIFY_INTERVAL`, disabled with `THREADWEAVE_NOTIFY_ENABLED=0`), resolves the author's email to their AAD id through Graph, and sends through the stored conversation reference (`~/.threadweave/bot_conversations.json`).
- People who never talked to the bot have no reference to send to, so their notifications stay pending, and a notice that keeps failing is marked skipped after `THREADWEAVE_NOTIFY_MAX_ATTEMPTS`. Nothing goes to the wrong person.

### 4. Opt-out (per person, everywhere)

Any person can decline harvesting. Their identity is registered once and checked at every capture path:

- **Central gate:** `/api/v1/ingest` step 0 rejects content attributed to an opted-out person (`id=opted_out`, nothing stored). This covers every connector, because every connector flows through ingest.
- **Early skip:** daemons do not even extract content from opted-out people (email: any thread participant; SharePoint: file `createdBy` email; Teams: message author).
- **Teams commands:** in a DM or via @mention, say:

| Command | Effect |
|---|---|
| `opt out` | Stop harvesting my content |
| `opt in` | Resume harvesting |
| `delete <topic>` | Delete my entries matching a topic |
| `status` | Show my privacy status |

- **Admin API:** `GET /api/v1/optout` (list), `POST /api/v1/optout/out` and `POST /api/v1/optout/in` (body `{"person": "email"}`). Storage: `~/.threadweave/optout.json`.

### 5. Right to delete (audited)

- `DELETE /api/v1/entries/{id}` removes an entry. Rights: the entry's author, a same-wing member for public/internal entries, or admin/legal/hr.
- Every deletion is written to the audit log (`action=delete`), and a refused attempt is written too. Deletions are permanent; the audit record is the guarantee that deletion happened.

## Access control (who can see what)

- **Wings and rooms** map to teams and topics; wing-scoped search isolates knowledge per team.
- **Seven sensitivity levels** (public, internal, confidential, restricted, hr_privileged, client_confidential, legal_privileged) gate search results and direct access. Clearance sets the ceiling, wing membership decides confidential and restricted, a client assignment decides client_confidential, a named person list decides restricted, and the hr and legal levels are held by their roles and wings.
- **Source boundaries outrank clearance** and no role bypasses them. Content captured from a Teams private channel is visible only to the members of that channel, and any source may carry an ACL of allowed and denied users or groups with a revocation stamp. Both fail closed: an empty member list or grant list means nobody is authorized.

## Deployment notes

- These stores persist across restarts: the entry store, the MemPalace index, the opt-out registry, the audit log and the notification queue.
- The entry store is write-through (`src/threadweave/store.py`, SQLAlchemy Core). SQLite at `~/.threadweave/entries.sqlite3` by default, PostgreSQL through `THREADWEAVE_ENTRY_DB`, reloaded into memory at startup and mirrored on every save and delete. A re-captured document becomes a new version of the earlier entry through `version_of`, chained for `GET /api/v1/entries/{id}/versions`.
- Deploy the API and the daemons on the on-prem host, and point the connectors and the bot at it on `localhost:8000`.
- Publish an internal policy describing what is watched, what is retained, and the opt-out and delete commands.

## This is a selling point

"ThreadWeave tells you what it remembers, lets you opt out, and lets you delete" is the product promise made visible. The privacy layer turns the on-prem architecture into something employees can trust.
