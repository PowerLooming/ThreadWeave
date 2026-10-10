# ThreadWeave Technical Specification

> Status: implementation reference for the code in this repository. Version,
> test counts and release history live in `pyproject.toml`, `README.md` and
> `CHANGELOG.md`.

## 1. System overview

ThreadWeave is a self-hosted organizational memory system. It pulls the decisions,
answers and runbooks out of the mail, chat and documents a team already writes, and
files them into a searchable store on hardware the organization controls. It depends
on MemPalace for storage and hybrid retrieval, and adds the ingest pipeline, the
confidentiality model, the org model, org-aware ranking and the interfaces in section
4. MIT licensed, Python 3.11 or newer.

Content moves one way. Every capture path is an outbound pull from the host
ThreadWeave runs on, and nothing in the capture or storage path can send content
anywhere. The one assistant interface is the MCP server, for agents the customer runs
on its own network. Every route that would put captured content in front of a cloud
assistant is closed by decision rather than left open, and
[ai-publication-boundary.md](ai-publication-boundary.md) is that rule.

```
   mail, chat and files                 ThreadWeave host
   (M365, Google Workspace,             +------------------------------------------+
    .eml, IMAP, Outlook COM)  ------->  | ingest: opt-out, dedup, detect, PII gate, |
          outbound pull only            | then store                                |
                                        | MemPalace drawers + durable entry store   |
                                        | search: hybrid, filtered, org re-ranked   |
                                        +---------+-----------------------+---------+
                                                  |                       |
                                       REST API, dashboard,        MCP server for agents
                                       CLI, Teams bot              on the customer's network
```

### 1.1 Capture paths that ship

| Path | Source | Entry point |
|------|--------|-------------|
| Microsoft 365 mail | Exchange Online mailboxes over Microsoft Graph, app-only credentials | `threadweave email watch`, `src/threadweave/connectors/email/` |
| SharePoint | Document libraries over Graph delta polling | `threadweave sharepoint watch`, `src/threadweave/connectors/sharepoint/` |
| OneNote | Notebooks over Graph, delegated device-code sign-in | `threadweave sharepoint watch --onenote`, `threadweave sharepoint onenote-login` |
| Teams | Channel messages over Graph delta polling, plus the bot for author-confirmed capture, privacy commands and capture notifications | `threadweave teams watch`, `src/threadweave/connectors/teams/` |
| Google Workspace | Gmail, Google Chat and Drive with a service account and domain-wide delegation | `threadweave gws sync`, `threadweave gws watch`, `threadweave gws harvest` |
| Exported mail | `.eml` files, IMAP mailboxes, and classic Outlook over COM | `ingest_emails.py`, `ingest_imap.py`, `ingest_outlook.py` |
| Inbox without an app registration | device-code Graph sign-in, MFA supported | `ingest_graph_mail.py` |

Every one of them ends at `POST /api/v1/ingest`, so every capture path is subject to
the same gate order in section 2. Connector setup and permissions:
[m365-connectors.md](m365-connectors.md).

Five daemons can be registered as operating system services with
`threadweave daemon install <name>`: `email-watch`, `sharepoint-watch`, `teams-bot`,
`teams-watch` and `org-sync` (`src/threadweave/daemons.py`).

## 2. Ingest pipeline

`POST /api/v1/ingest` is the single door into memory. The steps run in this order.

1. Tenant scoping. With auth enabled, the tenant carried by the API key overrides any
   tenant in the request body, so each tenant's memory stays separate.
2. Opt-out gate. Every identity the connector reports (author, sender, participants) is
   checked against the opt-out registry. One match and nothing is stored: the response
   comes back as `opted_out`.
3. Dedup. The key is `tenant_id|content|title|author_id`, hashed with SHA-256, so two
   messages with the same body but different subjects or senders are both kept. A
   repeat inside the same server run comes back as `duplicate`. The hash is recorded
   only when the entry is actually saved, so rejected or skipped content stays
   retryable.
4. Signature strip and detection. Signatures and disclaimers are cut before the
   detector sees the text, because they carry phone numbers and addresses by design.
   Detection then answers worth-saving, content type, entities, scope and language. The
   regex detector is the default; when `THREADWEAVE_LLM_BASE_URL` is set, an LLM pass
   runs first and regex remains the fallback. The worth-saving threshold is 0.40 in both
   engines. Content the detector reports as non-English is translated into `content_en`
   through the configured LLM backend, and a missing or failed translation never blocks
   the capture.
5. PII gate. Identifiers (national ID, card and bank account numbers, phone numbers,
   addresses, labelled person identifiers) are replaced with typed placeholders and the
   message is stored, which is the default (`THREADWEAVE_PII_MODE=redact`). `reject`
   restores the older destructive verdict and is still used for content whose substance
   is the identifiers. The redaction kinds and counts are recorded on the entry and in
   the decision audit.
6. Store. The entry is written to the entry store and as a MemPalace drawer, and the
   capture notification for its author is queued. Nothing is returned to the source.

## 3. Data models

### 3.1 Entry record (durable store)

`src/threadweave/store.py`, SQLAlchemy Core, SQLite by default and PostgreSQL when
`THREADWEAVE_ENTRY_DB` names one. The in-memory dicts are the source of truth during a
run, and the database is the durability mirror plus the reload source at startup.

| Column | Meaning |
|--------|---------|
| `id` | Entry id, also the MemPalace document id |
| `content` | Stored text, with identifiers redacted when the PII gate fired |
| `content_en` | English translation for content captured in another language |
| `wing`, `room` | Team that produced it, topic it belongs to |
| `scope` | `team`, `department` or `organization` |
| `source_type` | Where it came from: `email`, `teams`, `sharepoint`, `onenote`, `gws`, `manual`, `api` |
| `author_id`, `title` | Author and title, both part of the dedup key |
| `created_at` | When the source text was written |
| `entities` | Extracted entities, JSON |
| `content_type` | `answer`, `decision`, `question`, `chat` or `reference` |
| `has_pii` | Whether the PII gate fired |
| `tenant_id` | Tenant the entry belongs to, `default` when auth is off |
| `sensitivity` | One of the seven confidentiality levels in section 7.1 |
| `client_id` | Client engagement for `client_confidential` entries |
| `allowed_people` | Named people ACL for restricted entries, JSON |
| `acl` | Per-entry access record, JSON |
| `refinement_notes` | Notes added when an entry is refined, JSON |
| `version_of` | Entry this one replaces, which is what makes a provenance chain |

### 3.2 MemPalace drawer metadata

`MemPalaceClient.add_drawer()` stores the verbatim text under the keys `wing`, `room`,
`source_file`, `title`, `created_at`, `author_id`, `content_type`, `tenant_id` and
`sensitivity`, and drops empty ones. Tenant and sensitivity travel with the drawer so
search can filter on them. Passing `drawer_id` keeps the MemPalace document id equal to
the ThreadWeave entry id.

### 3.3 Org model (temporal triples)

`src/threadweave/org_model.py` keeps entities of type `person`, `team`, `domain` or
`role`, and relationships carrying `valid_from` and an optional `valid_to` (`None` means
still current). The known relations are `member_of`, `reports_to`, `owns`,
`collaborates_with`, `succeeded_by` and `subteam_of`. With a knowledge graph path the
model persists into MemPalace's `KnowledgeGraph`; without one it is in-memory only.

### 3.4 Detection result

`src/threadweave/detector.py`:

```python
class ContentType(Enum):
    ANSWER = "answer"
    QUESTION = "question"
    CHAT = "chat"
    DECISION = "decision"
    REFERENCE = "reference"

@dataclass
class DetectionResult:
    content_type: ContentType
    confidence: float
    signals: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    suggested_scope: str = "team"
    suggested_title: str = ""
    has_pii: bool = False
    has_gossip: bool = False
    language: str = ""          # ISO 639-1, empty when unknown or English
```

### 3.5 Relevance score

`src/threadweave/relevance.py`. The four factors combine with the weights in
`DEFAULT_WEIGHTS`: semantic 0.40, org proximity 0.30, freshness 0.20, authority 0.10.

```python
@dataclass
class RelevanceScore:
    semantic: float = 0.0       # 0.0-1.0, from vector search
    org_proximity: float = 0.0  # 0.0-1.0, same team at the time = 1.0
    freshness: float = 0.0      # 0.0-1.0, brand new = 1.0, years old = low
    authority: float = 0.0      # 0.0-1.0, domain expert = high
    combined: float = 0.0
```

## 4. Interfaces

### 4.1 REST API

FastAPI. The running server serves its full OpenAPI schema at `/docs`, which is the
authoritative list; the table below names the endpoints a reviewer or an integrator
reaches for first.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Status, version, detector in force, entries stored |
| `POST` | `/api/v1/ingest` | The pipeline in section 2 |
| `POST` | `/api/v1/detect` | Classify text: worth saving, type, PII, gossip, language |
| `POST` | `/api/v1/search` | Hybrid search, confidentiality-filtered, re-ranked |
| `POST` | `/api/v1/entries` | Save an entry directly |
| `GET` | `/api/v1/entries/{entry_id}` | One entry in full |
| `DELETE` | `/api/v1/entries/{entry_id}` | Audited deletion (right to delete) |
| `GET` | `/api/v1/tasks` | Open action items |
| `GET` | `/api/v1/wings` | Wings with entry counts |
| `GET` | `/api/v1/wings/{wing}/rooms` | Rooms carrying entries in a wing |
| `POST` | `/api/v1/org/relationships` | Add an org relationship |
| `GET` | `/api/v1/org/people/{person_id}/team` | A person's team, optionally as of a date |
| `GET` | `/api/v1/org/graph` | Org graph, hallway and tunnel edges |
| `GET` | `/api/v1/audit/recent` | Audit log |
| `GET` | `/api/v1/optout` | Opt-out registry; `POST /optout/out` and `/optout/in` change it |

Auth is opt-in (`THREADWEAVE_REQUIRE_AUTH=1`) and key-based. `/health`, `/metrics` and
`/metrics/prometheus` stay open.

### 4.2 MCP server

`src/threadweave/mcp_server.py` exposes nine tools over Streamable HTTP, each of them a
call to the REST API carrying the caller's own key: `search_memory`, `get_decisions`,
`get_entry`, `entry_provenance`, `who_knows`, `list_open_commitments`, `classify_text`,
`capture_knowledge` and `browse_topics`. Tenant scoping, confidentiality, the opt-out
registry and the audit trail stay in the layer that already owns them, so an agent
cannot bypass a rule a person is subject to. The tool contract, the authentication and
the list of cloud assistant routes that are closed are in
[mcp-server.md](mcp-server.md).

### 4.3 Dashboard

One page (`src/threadweave/static/index.html`) served at `/` by the API: pipeline
metrics, search, a save form, recent entries, and the org graph drawn with D3 from
`GET /api/v1/org/graph`.

### 4.4 CLI

`threadweave` with the verbs `detect`, `search`, `save`, `demo`, `serve` and `mcp`;
`email login|watch`, `sharepoint watch|onenote-login`, `teams package|publish|watch`,
`gws check|sync|watch|harvest|harvest-report|onboard`, `org sync`,
`daemon run|install|uninstall|status|config`, and
`tasks list|team|done|undone|reassign|digest`.

## 5. MemPalace integration

ThreadWeave depends on `mempalace>=3.5.0` and does not fork it. What the code imports:

| MemPalace component | Used for |
|---------------------|----------|
| `mempalace.palace.get_collection` | Drawer storage and deletion |
| `mempalace.searcher` (`build_where_filter`, `_bm25_scores`, `_hybrid_rank`) | Hybrid keyword plus vector retrieval |
| `mempalace.knowledge_graph.KnowledgeGraph` | Temporal org model triples |

The default backend is Chroma on disk under the palace path, and the backend and the
embedding model are MemPalace settings. The default embedding model is
`embeddinggemma-300m` (ONNX, 384 dimensions) and it runs on the host, so no inference
service is called.

## 6. Deployment

### 6.1 Docker Compose

`docker compose up` on one machine starts the `api` service (port 8000, health check on
`/api/v1/health`), with two named volumes for the palace and the entry store, and the
key file mounted read-only from `~/.threadweave/keys.json`. `docker compose --profile llm
up` adds a local Ollama for LLM detection. All of it sits on one host behind the
organization's firewall.

### 6.2 Native Python

Python 3.11 or newer and `uv`: `bash setup.sh`, or `uv venv` plus
`uv pip install -e ".[dev]"`. Optional extras carry the connector, MCP and driver
dependencies (`gws`, `graph`, `email`, `sharepoint`, `teams`, `outlook`, `mcp`,
`decisions`, `laya`, `postgres`, `all-connectors`, plus `dev`), so a plain install
serves the API and CLI without them.

### 6.3 On-premises and air-gapped

Detection, redaction, storage and search all run on the host. The only outbound traffic
is what the connectors make to Microsoft 365 and Google Workspace in order to read, plus
the LLM base URL if an operator points one somewhere. State lives under
`~/.threadweave` (entry store, audit log, opt-out registry, notification queue, keys)
and in the palace path.

## 7. Security and privacy

### 7.1 Confidentiality

Seven levels, ordered from least to most restrictive: `public`, `internal`,
`confidential`, `restricted`, `client_confidential`, `hr_privileged`,
`legal_privileged` (`src/threadweave/confidentiality.py`). A level is detected at ingest
and enforced at read, and it gates both search results and direct reads. A stored value
the enum does not recognise resolves to `restricted`, which requires a named ACL, so a
typo hides content rather than exposing it.

### 7.2 Access control and identity

Wing membership, named person ACLs on restricted entries, and the special HR and legal
wings decide who can see what, and `can_see()` denies on anything it cannot resolve.
When auth is on, the API key is the identity: keys in `~/.threadweave/keys.json` may
carry `tenant_id`, `role`, `wing` and `person_id`, and identity claims in a request body
are honoured only while auth is off.

### 7.3 PII handling

Detection runs before storage, and the identifier scan is fused into every detector
verdict, so the regex path, the modelled path and `/api/v1/detect` all agree on what
counts as PII. Redaction is the default outcome, and deletion is confined to content
whose substance is the identifiers. The design, the measurements and the known limits,
including the semantic half of PII that patterns cannot see, are in
[pii-gate-redaction.md](pii-gate-redaction.md).

### 7.4 Audit

The audit log records confidential-and-above reads, denied attempts and deletions, each
with requester, entry, sensitivity, wing and tenant, and folds repeated identical events
into one row with a count so a retrying client cannot bury the distinct ones. A public or
internal read is not recorded. Per-person opt-out is checked at ingest and inside the
daemons, and deletion is available to the author, to a same-wing colleague for public and
internal entries, and to the admin, HR and legal roles.

### 7.5 Data residency

All state stays on the host that runs ThreadWeave. The code ships no telemetry and no
usage reporting, and nothing is sent to an external AI service.

## 8. What this repository does not ship

- No SSO. There is no LDAP, SAML or OIDC client, and identity is the API key (section
  7.2) rather than a directory account.
- No Kubernetes manifests. Compose on one host and a native install are the deployment
  shapes that exist.
- No publisher. Nothing in the capture or storage path can send content outward, the
  Microsoft 365 Copilot (Graph) connector that did is removed, and the rule for any
  future outbound surface is [ai-publication-boundary.md](ai-publication-boundary.md).
- No webhook receiver. Capture is pull polling end to end: no daemon and no API route
  accepts a push notification, and no connector code path creates a Graph subscription.
