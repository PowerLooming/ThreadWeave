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

## The private link (P2)

A peer key over the public internet is not the design. The link comes first, the
key second: the instance is reachable from the partner's network and from
nowhere else, and the transport authenticates both ends before a key is read.

```
threadweave peer tls init --host weave.internal --peer nordvik   # add --host for every name and address it answers to
threadweave peer tls status
threadweave serve --host 0.0.0.0 --port 8443 --tls
```

`init` creates a private certificate authority, one server certificate carrying
every name and address the instance answers to, and one client certificate per
partner. Send `ca.pem` to both sides, one `client-<peer>.pem` plus its key to
each partner, and never move `ca.key` off the instance: it can mint a
certificate for any peer.

`serve --tls` requires a client certificate. A client without one is refused at
the handshake, before any routing, any key check or any content. The directory
defaults to `~/.threadweave/peer-tls` and can be moved with
`THREADWEAVE_PEER_TLS_DIR`.

The partner's side is one command:

```
threadweave peer check https://weave.internal:8443 --key sk-peer-... --query "release process" \
    --ca ca.pem --cert client-nordvik.pem --cert-key client-nordvik.key
```

### Rate limit

A key is a standing grant, so it is rate limited per partner:
`THREADWEAVE_PEER_RATE_LIMIT` requests per minute, default 60, `0` disabled. The
bucket refills every second, so a steady trickle is never pushed to a next
window by one burst, and the ceiling still holds over time. Over the limit is
`429` with `Retry-After: 60`, and ordinary keys are not affected.

### The acceptance run

Proven on one host between two endpoints, and to be repeated with a second
machine before a link opens for real:

| # | check | expected |
|---|---|---|
| 1 | a request with no client certificate | refused at the handshake |
| 2 | a request with the partner certificate | `200` |
| 3 | the same with a certificate from another authority | refused at the handshake |
| 4 | a client trusting another authority | refuses this server |
| 5 | a peer search with `limit: 50` | only its wing, at most its cap |
| 6 | a peer read in its wing / outside it | `200` / `404`, with a `denied` record |
| 7 | a peer request outside the allowlist | `403` |
| 8 | requests past the rate limit | `429` with `Retry-After` |
| 9 | the audit trail | `peer_search` (digest and count), `peer_read`, `denied` |
| 10 | `peer revoke`, then the next request | `403 Invalid API key`, no restart |

Two host-level notes from running it on Windows:

- **The curl that ships with Git for Windows cannot present a PEM client
  certificate**: it is built against Schannel, which wants a certificate from
  the Windows store, and it fails with exit 58. Use `threadweave peer check`, or
  a curl built against OpenSSL, on the partner side. On Linux and macOS curl
  works as written.
- Servers and clients on the same machine still exercise the whole path if you
  bind the instance to the machine's own LAN address and dial that address, which
  is what the accepted run above did.

## Honest limits

- **No-copy is a promise, not a mechanism.** The API hands over content to a
  partner's client; nothing stops that client from storing it. The no-copy
  property belongs in the partnership terms, and the audit trail is what makes
  it checkable.
- **The MCP server is gated too** (search and a single entry read, nothing
  else), so a peer key is held to the same two capabilities on both surfaces.
- **The wing is the scope, and it must exist.** A peer key scoped to a wing that
  does not exist can read nothing, which is the correct failure, but it also
  looks like a broken integration. Check the wing name before issuing.
