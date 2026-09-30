# MCP server — ThreadWeave as agent tools

ThreadWeave exposes its memory over the Model Context Protocol (Streamable
HTTP), so an AI agent can search the organization's decisions, read the
provenance behind them, list open commitments, classify text before capture,
and write down what it concluded. Any MCP client can use it: Copilot Studio
agents, Microsoft 365 Copilot through a federated connector, GitHub Copilot,
Claude Code, or your own client.

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
A shared agent key sees what that key is cleared for. Per-user identity
(Entra OAuth, on-behalf-of) is the next step for the M365 Copilot federated
connector path.

## Connect a Copilot Studio agent

1. In Copilot Studio, open your agent, then **Tools** > **Add a tool** >
   **New tool** > **Model Context Protocol**.
2. Fill in **Server name** (`ThreadWeave`), **Server description** (this is
   what the orchestrator matches on, so say "organizational memory: decisions,
   commitments and who knows what"), and **Server URL**
   (`https://<your-host>/mcp`).
3. Authentication: **API key**, type **Header**, header name `X-API-Key`.
4. **Create**, add the connection, and the agent now sees the nine tools.
5. Publish the agent. Power Platform data policies apply to MCP servers
   (connectivity goes through Power Platform connectors), so an admin has to
   allow the endpoint.

Only the Streamable HTTP transport is supported by Copilot Studio; SSE was
dropped in August 2025, which is what this server speaks.

## Microsoft 365 Copilot (federated connector)

Microsoft 365 Copilot connectors have two models. Synced connectors index
content into Microsoft Graph; **federated connectors** fetch over MCP in real
time and index nothing. For ThreadWeave the federated model is the one that
matches the on-prem contract: the content stays in the source.

- Federated connectors are federated per user: user credentials, not admin
  credentials, so each user connects their own identity.
- Read works today; write, update and delete actions are rolling out from
  early October 2026.
- The connector is governed in the Microsoft 365 admin center
  (**Copilot connectors** > **Your connections**), and an internal server can
  be registered for centralized governance through the Bring Your Own (BYO)
  MCP server path (preview) using the Agent 365 CLI, after which admins
  approve it in **Agents** > **Tools**.

The synced Graph connector that used to live in `connectors/graph`, driven by
`threadweave graph ...`, has been removed. It pushed captured entries into the
tenant's search index from a daemon inside the capture path, which contradicts
the one-way promise in [privacy.md](privacy.md), and it cannot be tested without
a Copilot licence. The federated model above is the path that remains: nothing is
indexed, and the assistant asks instead. Any future outbound surface has to be a
deliberate, per-caller publication rather than a daemon, as
[ai-publication-boundary.md](ai-publication-boundary.md) sets out.

## Reaching an on-prem server

Copilot cannot call a private endpoint; the MCP URL must be reachable over
HTTPS from the service. Keep the inbound path narrow:

- Put a broker in front: Azure API Management with the on-prem backend over
  a private route, or the Agent 365 Tooling Gateway (BYO MCP server).
- Publish under a hostname the server accepts. The SDK's DNS-rebinding guard
  admits loopback only, so a published endpoint answers **421 Invalid Host
  header** until the name is allowlisted.

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
