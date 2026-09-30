# The personal profile (single user)

ThreadWeave runs two profiles from one codebase. The managed profile is the multi-user
deployment described in [m365-connectors.md](m365-connectors.md). The personal profile is the
same installation narrowed to one person: one mailbox, one owner, everything on that person's
own machine.

It is not a fork, a separate build, or a desktop app. The store, the detection pipeline, the
confidentiality levels, the audit log and the privacy contract are the same code. What changes
is who consents and how many users, which is configuration rather than a codebase.

## What the profile changes

| | managed | personal |
|---|---|---|
| selected by | default | `THREADWEAVE_PROFILE=personal` |
| identity | many users, each with a key | one owner, `THREADWEAVE_OWNER_ID` (default `owner`) |
| mail access | app-only, client credentials, a configured mailbox | delegated, `/me`, the signed-in owner's own mailbox |
| secret needed | yes, a client secret | no, and no application permission |
| captures filed under | tenant `default` | tenant `personal` |
| connectors offered | email, SharePoint, Teams, org harvesters | the email watcher only |
| grants on a capture | resolve against the directory | resolve to the owner; group entries cannot be satisfied, because the tier performs no directory lookups |

Captures go to the `personal` tenant because a search that names a tenant excludes every other
one, so filing them under `default` would hide the owner's own mail from the owner's own
searches.

## What you need

- Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- a Microsoft 365 mailbox
- one Entra app registration with **Allow public client flows** set to Yes and the delegated
  `Mail.Read` permission. There is no client secret and no application permission. Admin
  consent is needed only if your tenant blocks user consent
- a machine to run it on

## Setup

```bash
git clone https://github.com/PowerLooming/ThreadWeave.git
cd ThreadWeave
uv pip install -e ".[dev]"

export THREADWEAVE_PROFILE=personal

# One-time device-code sign-in. Prints a code and a URL; open
# https://microsoft.com/devicelogin, enter the code, sign in. The token is
# cached at ~/.threadweave/msal_cache.json and refreshed silently after that.
threadweave email login

# The API server
threadweave serve

# The watcher: reads your own mailbox over /me and files captures in the
# personal tenant. In this profile `--mail-auth` defaults to delegated, so no
# mailbox and no secret are named.
threadweave email watch --interval 300
```

To have the watcher start at login rather than in the foreground:

```bash
threadweave daemon install email-watch   # Windows Startup launcher, or a systemd unit
threadweave daemon status email-watch
```

Check it is alive with `curl http://localhost:8000/api/v1/health`.

## What it does not do

- no Teams bot, no SharePoint or OneNote polling, no org-wide harvesters, and therefore no
  RSC consent, no admin grants and no directory-wide permissions
- no sharing between people: the owner is the one reader, and the profile performs no directory
  lookups to resolve grants
- no installer, no tray app, no desktop UI; it is the same stack run in a narrower profile
- not published to PyPI. The `threadweave` package on PyPI is an unrelated email-threading
  library, so install from the repository rather than from an index

## The boundary is unchanged

The one-way contract holds in this profile as it does in the managed one: content flows from
Microsoft 365 to the on-prem host and nothing in the capture path publishes outward. See
[privacy.md](privacy.md) for the contract, [pii-gate-redaction.md](pii-gate-redaction.md) for
what is redacted before storage, and [ai-publication-boundary.md](ai-publication-boundary.md)
for the rule that any future AI-facing surface must be a deliberate, per-caller, audited
publication rather than a daemon.
