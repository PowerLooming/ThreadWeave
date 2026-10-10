# MCP server — ThreadWeave as agent tools

ThreadWeave exposes its memory over the Model Context Protocol (Streamable
HTTP), so an AI agent you run on your own network can search the
organization's decisions, read the provenance behind them, list open
commitments, classify text before capture, and write down what it concluded.
The client is yours: an agent process on the same host, a client elsewhere on
your internal network, Claude Code, or your own code. Cloud assistant hosts are
closed by decision, and a later section says why.

Two commands run the whole thing:

```bash
pip install "threadweave-memory[mcp]"   # or, in a uv checkout: uv sync --extra mcp
threadweave serve            # the REST API + store  (http://127.0.0.1:8000)
threadweave mcp              # the agent tool surface (http://127.0.0.1:8100/mcp)
```

The MCP SDK is an optional extra, so a plain install serves the API, the
connectors and the bots without it.

## Why a shim, not a second memory

Every tool call is an HTTP request to the ThreadWeave API carrying the
caller's own API key. Tenant scoping, confidentiality ACLs, the gossip gate,
sensitivity detection, the opt-out registry and the audit trail therefore
stay in the layer that already owns them, and an agent read is audited
exactly like a human read. Nothing about capture is reimplemented here, so an
agent cannot bypass a rule that a person is subject to.

## Tools

| Tool | What it answers | API call |
|------|-----------------|----------|
| `search_memory` | What do we know about X (filter by team, topic or content type) | `POST /api/v1/search` |
| `get_decisions` | What did we decide, and who decided it | `POST /api/v1/search` filtered to `decision` |
| `get_entry` | One entry in full, by id | `GET /api/v1/entries/{id}` |
| `entry_provenance` | How do we know this: version chain + access audit | `/entries/{id}/versions`, `/audit/entry/{id}` |
| `who_knows` | Who has written about X, in which teams | derived from search hits |
| `list_open_commitments` | What is outstanding, by owner, with deadlines | `GET /api/v1/tasks` |
| `classify_text` | Is this knowledge, what type, gossip, PII, sensitivity | `/api/v1/detect`, `/api/v1/detect-sensitivity` |
| `capture_knowledge` | Write a decision, commitment or note into memory | `POST /api/v1/entries` |
| `browse_topics` | What subjects exist in memory at all | `POST /api/v1/topics` |

Search results carry a citation URL back to the exact captured source
(Teams message, mail, file), so an agent answer can be checked rather than
trusted.

## Authentication

The same key store as the REST API, same opt-in gate:

- `THREADWEAVE_REQUIRE_AUTH=1` enforces keys on the MCP endpoint too.
- The client sends `X-API-Key` (or `Authorization: Bearer`) on every request,
  and that key is forwarded to the REST API untouched.
- Keys in `~/.threadweave/keys.json` may carry `person_id` and `wing`, which
  becomes the requester identity for ACL checks. One key per agent, or one
  per user where the host can hold per-user credentials.

Note the current limit: identity is the key, not a per-user Entra token.
A shared agent key sees what that key is cleared for, and per-user identity
(Entra OAuth, on-behalf-of) is not implemented.

## Point a client at the server

An MCP client needs three things and nothing else: the URL
(`http://<host>:8100/mcp`), the Streamable HTTP transport, and the caller's key
in the `X-API-Key` header. Client configuration formats differ, so follow your
client's own MCP settings page; the server side is identical for all of them.
The SDK speaks Streamable HTTP only, so a client that insists on SSE cannot
connect.

## Cloud assistants: closed by decision

The MCP server is for agents you run on your own network. Every route that
would put this memory in front of a cloud assistant is closed, and the closure
is a decision rather than a gap:

- The synced Microsoft 365 Copilot (Graph) connector used to live in
  `connectors/graph`, driven by `threadweave graph ...`. It pushed captured
  entries into the tenant's search index from a daemon inside the capture path,
  which contradicts the one-way promise in [privacy.md](privacy.md), and it
  cannot be tested without a Copilot licence. The connector and its CLI verbs
  are removed.
- The federated MCP connector is closed for the same reason the whole boundary
  exists. A cloud assistant cannot call a private endpoint, so reaching this
  server would mean exposing it through a broker, and a federated connector
  still grounds its answers in the tenant's cloud.
- A Copilot Studio agent and a Power Platform custom connector are cloud hosts
  as well, so they are closed with the rest.

What remains is the interface on this page: pull, the caller's own key,
filtered for that caller, audited like a read. Rule:
[ai-publication-boundary.md](ai-publication-boundary.md). Any future outbound
surface has to be a deliberate, operator-invoked publication rather than a
daemon.

## Reaching the server from your internal network

The MCP server binds loopback by default, and the SDK's DNS-rebinding guard
admits loopback host names only. A server reached under any other name answers
`421 Invalid Host header` until the name is allowlisted, so set the allowlist
to the name your own agents use on the internal network:

```bash
export THREADWEAVE_MCP_ALLOWED_HOSTS="mcp.example.com,mcp.example.com:443"
export THREADWEAVE_MCP_ALLOWED_ORIGINS="https://mcp.example.com"   # only if a browser origin calls it
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `THREADWEAVE_API_BASE_URL` | `http://127.0.0.1:8000` | REST API the tools call |
| `THREADWEAVE_API_KEY` | unset | Key used when the MCP client sends none |
| `THREADWEAVE_REQUIRE_AUTH` | off | Enforce API keys on both surfaces |
| `THREADWEAVE_MCP_ALLOWED_HOSTS` | loopback only | Host allowlist for the MCP endpoint |
| `THREADWEAVE_MCP_ALLOWED_ORIGINS` | none | Origin allowlist for browser clients |

CLI flags: `threadweave mcp --host --port --api-url`.

## Try it without an agent

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

With auth on, add `-H "X-API-Key: <key>"`.

## Test coverage

`tests/test_mcp_server.py` drives the real wire protocol (initialize,
tools/list, tools/call over SSE-framed responses) against the real REST API
in-process, covering the tool contract, capture/search round-trip, decision
filtering, the gossip refusal, tenant scoping under auth, the host allowlist,
and the unreachable-API path.
