# Peer keys

A peer key lets another organisation read from this instance. It is the one
sanctioned cross-organisation path: nothing is copied, nothing is pushed, and
every read carries the peer's name in the audit log.

Read `docs/ai-publication-boundary.md` first. Rule R8 defines it, rule R9 keeps
replica sync inside one legal entity.

## Issue one

```
threadweave peer issue nordvik --wing engineering \
    --contact security@example.invalid --issued-by harald --max-results 25
```

The key is printed once, exactly like a password. Copy it into the partner's
agent, then forget it: `threadweave peer list` never shows key values, so if it
is lost, revoke and issue again.

Issuance requires a `peer_id` and a wing, nothing else. Defaults are a
three-month expiry and a cap of 25 results per query, both recorded in the
record so the owner can see what was granted without reading the key file.

## What the peer may do

| allowed | refused |
|---|---|
| `POST /api/v1/search`, one wing, capped results | everything else in the API: entries listing, recap, tasks, wings, tenants, org graph, audit, ingest, save, delete |
| `GET /api/v1/entries/{id}` for an entry in its wing | an entry outside the wing (404, and a `denied` audit record) |

The allowlist lives in one place, `auth.PEER_ALLOWED_ROUTES`, and a test walks
every route in the app and asserts the refusal, so adding a route cannot quietly
widen a peer's reach.

## What the owner sees

| action | recorded |
|---|---|
| `peer_search` | the query's sha256 digest and the number of results returned, never the query text |
| `peer_read` | the entry id and wing, the peer's name, whether or not the entry is sensitive |
| `denied` | a read outside the wing, with the reason |

The audit names the peer as `peer:<peer_id>`, so `get_for_requester("peer:nordvik")`
answers "everything this partner read" without joining tables.

## Revoke

```
threadweave peer revoke nordvik      # every active key for that peer
threadweave peer list                # shows active / revoked, expiry, wing, cap
```

Revocation is effective on the next request: the key fails validation, so the
peer gets `403 Invalid API key`.

## Check one from the other side

```
threadweave peer check https://partner.internal:8000 --key sk-peer-... --query "release process"
```

Send your own key one question and see what comes back, count and all.

## Honest limits

- **No-copy is a promise, not a mechanism.** The API hands over content to a
  partner's client; nothing stops that client from storing it. The no-copy
  property belongs in the partnership terms, and the audit trail is what makes
  it checkable.
- **The MCP server is not gated yet.** A peer key used over MCP can currently
  reach the MCP tools, which are broader than the HTTP allowlist. Do not hand a
  peer key to an MCP client until that seam is closed.
- **The wing is the scope, and it must exist.** A peer key scoped to a wing that
  does not exist can read nothing, which is the correct failure, but it also
  looks like a broken integration. Check the wing name before issuing.
