# ThreadWeave Privacy Model

ThreadWeave captures organizational knowledge so nobody has to ask Lars. Passive capture without disclosure is surveillance, so the privacy model is a first-class feature, not a footnote.

## The contract

**Content flows ONE WAY: Microsoft 365 → on-prem ThreadWeave. It never leaves to a third party, and it never comes back.**

- All connectors use outbound pull polling (Graph API from the on-prem host). No webhooks, no tunnels, no third-party relays carry content.
- Processing runs entirely on-prem: detection, PII screening, storage (MemPalace), and the LLM.
- Nothing is sent to external AI services. Ever.

## The "camera sign": four layers

### 1. Transparency (what we watch, what we captured)

- Every entry records provenance: `source` (teams / email / sharepoint / onenote), `author_id`, `source_file`, `created_at`, and wing/room.
- The audit log (`GET /api/v1/audit/recent`, SQLite at `~/.threadweave/audit.sqlite3`) records every sensitive access, denied attempt, and deletion.
- The dashboard shows what the system watches and what it has captured.

### 2. Consent at capture (Teams)

- The Teams bot never stores anything silently: it detects knowledge, offers an Adaptive Card, and saves only when the author clicks **Save**.
- Email, SharePoint, and OneNote are automatic by design (there is no human at capture time), which is why layers 3 and 4 exist.

### 2b. Capture notification (the sign that talks)

When a daemon saves knowledge from someone's content, that person gets a **Teams DM from the bot**:

> **Captured to the palace** — your email "root cause" was added (wing: email). Say **delete root cause** to remove it, or **opt out** to stop future captures.

- Notifications are queued **only for the content author** (`~/.threadweave/notifications.sqlite3`), never for unrelated people.
- Opted-out authors never generate entries, so they never generate notifications.
- Delivery: the bot polls `GET /api/v1/notifications/pending` every 60s (configurable: `THREADWEAVE_NOTIFY_INTERVAL`, disable with `THREADWEAVE_NOTIFY_ENABLED=0`), resolves the author's email to their AAD id via Graph, and sends a proactive DM through the stored conversation reference (`~/.threadweave/bot_conversations.json`).
- People who never talked to the bot can't be DM'd — their notifications stay pending (by design; nothing leaks to the wrong person).

### 3. Opt-out (per person, everywhere)

Any person can decline harvesting. Their identity is registered once and checked at every capture path:

- **Central gate:** `/api/v1/ingest` step 0 rejects content attributed to an opted-out person (`id=opted_out`, nothing stored). This covers every connector, because every connector flows through ingest.
- **Early skip:** daemons do not even extract content from opted-out people (email: any thread participant; SharePoint: file `createdBy` email).
- **Teams commands:** in a DM or via @mention, say:

| Command | Effect |
|---|---|
| `opt out` | Stop harvesting my content |
| `opt in` | Resume harvesting |
| `delete <topic>` | Delete my entries matching a topic |
| `status` | Show my privacy status |

- **Admin API:** `GET /api/v1/optout` (list), `POST /api/v1/optout/out` and `POST /api/v1/optout/in` (body `{"person": "email"}`). Storage: `~/.threadweave/optout.json`.

### 4. Right to delete (audited)

- `DELETE /api/v1/entries/{id}` removes an entry. Rights: the entry's author, a same-wing member for public/internal entries, or admin/legal/hr.
- Every deletion is written to the audit log (`action=delete`). Deletions are permanent; the audit record is the guarantee that deletion happened.

## Access control (who can see what)

- **Wings and rooms** map to teams and topics; wing-scoped search isolates knowledge per team.
- **Sensitivity levels** (public, internal, confidential, restricted, hr_privileged, client_confidential, legal_privileged) gate search results and direct access.
- **Person-level ACLs** on restricted entries; special wings (HR, legal) are gated to their roles.

## Deployment notes

- The opt-out registry and audit log persist across restarts; the entry store itself is in-memory (planned: SQLite-backed persistence).
- Deploy the API and daemons on the on-prem host; point connectors at `localhost:8000`.
- Publish an internal policy describing what is watched, retention, and the opt-out/delete commands, then point to it from the dashboard.

## This is a selling point

"ThreadWeave tells you what it remembers, lets you opt out, and lets you delete" is the product promise made visible. The privacy layer turns the on-prem architecture into something employees can trust.
