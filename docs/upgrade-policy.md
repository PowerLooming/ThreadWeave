# Upgrade and deprecation policy

What version numbers mean, how a release is cut, what happens when a connector's
vendor API breaks, and how much notice a removal gets. Read this before pinning a
version in a pilot.

## Versioning

ThreadWeave follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `pyproject.toml`, `GET /api/v1/health` reports it, and
`CHANGELOG.md` has one section per release in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form. Releases are
tagged `vX.Y.Z`, and the latest tag is what the README badge shows.

| Bump | What it means | What you do |
|---|---|---|
| patch, `0.4.26` to `0.4.27` | fixes, connector repairs, internal changes | upgrade; no configuration change expected |
| minor, `0.4.x` to `0.5.0` | a new capability, connector or option | upgrade; existing configuration and API calls keep working |
| major, `0.x` to `1.0` | a break: a renamed environment variable, a removed CLI verb or endpoint, or a store change that needs action | read the release notes before upgrading; the change and the migration are named there |

`tests/test_version_consistency.py` fails the build when `pyproject.toml`, the
API health version and the changelog disagree, so a release cannot ship with a
version that lies about the tree.

## How a release is cut

Releases are automatic. `.github/workflows/auto-tag-release.yml` runs on every
push to master:

- if code changed since the latest tag, it increments the patch version, tags it,
  and publishes the GitHub Release with generated notes
- a manual minor or major bump in `pyproject.toml` is honoured as written
- a docs-only push, meaning only `docs/`, `README`, `CHANGELOG`, `.github/` or
  any `.md` file, produces no release

So every code change that reaches master produces a release and a changelog
entry. There is no separate "supported release" to wait for.

## Upgrading

```bash
# 1. stop what is running
systemctl --user stop threadweave-email-watch.service   # repeat per daemon (Linux)
#    Windows: the launchers start at sign-in, so sign out or end the process
#    in Task Manager, and close the `threadweave serve` window
# 2. take a copy of the entry store and the palace directory
# 3. update the code
git pull
uv sync                            # Docker: docker compose pull && docker compose up -d
# 4. start the API again, then the daemons
threadweave daemon status all
curl -s localhost:8000/api/v1/health      # the version field confirms the new code is running
```

- The entry store migrates itself at startup. `store.py` creates the table if it
  is missing and adds any missing column with `ALTER TABLE ... ADD COLUMN`, so an
  existing SQLite or PostgreSQL store keeps its data across an upgrade. No manual
  migration step ships.
- The palace index, the audit log, the opt-out registry, the notification queue
  and the daemon state files keep their paths, so an upgrade does not move them.
- Daemons execute the installed package (`<venv python> -m threadweave.cli daemon
  run <name>` under systemd, a startup launcher on Windows), so restarting them
  is what runs the new version. Reinstalling with `threadweave daemon install
  <name>` is only needed when a release note says the launcher changed.
- There is no downgrade path. An older version pointed at a store a newer version
  has written to is outside what the project verifies, so keep the copy from step
  2 until the new version has run cleanly for a while.

## What happens when a connector's vendor API breaks

The connectors call Microsoft Graph and Google's APIs directly. There is no
compatibility shim in front of them, because a shim would be a second
implementation to keep correct, and the vendor is the source of truth for what
the endpoint accepts.

When a vendor changes something a connector depends on, this is the sequence:

- Capture for that one connector stops. The daemon catches the error at each
  poll, logs `Poll failed: ...` to `~/.threadweave/logs/<name>.log`, increments
  its error count and keeps polling at its interval. It does not crash and it
  does not silently exit, so the process stays visible in `daemon status all`
  while capture is not happening.
- Everything else keeps working. The API, search, the other connectors and the
  stored history are unaffected.
- The project reproduces it, fixes it and ships it in a patch release with a
  release note naming the cause. Vendor-side permission changes are documented
  with the permission that has to be granted, because that part is the
  customer's to do. Microsoft retired app-only OneNote tokens on 2025-03-31 and
  the connector moved to delegated authentication, which is the shape of that
  kind of change ([m365-connectors.md](m365-connectors.md)).
- If a vendor deprecation has no replacement, the release notes say so. A
  connector is never described as working when it is not.

There is no built-in alerting yet: no page, no webhook and no watchdog that
notices a daemon has stopped capturing. Monitoring is the operator's, from the
log file, the process status and the API health endpoint. `GET /api/v1/health`
reports the API and its detector, not per-daemon liveness, so the log line is
the signal that a connector has broken.

There is no contractual fix time. The response targets are on the
[support page](support.md); a broken connector is treated as a defect, not a
feature request.

## Deprecation notice

For ThreadWeave's own capabilities, meaning a CLI verb, an environment variable,
an endpoint, a profile or a connector the project itself removes:

- A removal is announced in `CHANGELOG.md` under `Deprecated` and in the release
  notes at least one minor release before it happens. It never lands in a patch
  release.
- Minimum notice is 30 days or until the next minor release, whichever is
  longer.
- The exception is a change that closes a security hole or a way for content to
  leave the on-prem host. Those ship as soon as they are ready, in the closest
  release, and the note says why. The Copilot/Graph connector removal is the
  worked example: it was a boundary fix, it shipped without a deprecation
  period, and the reasoning is in
  [ai-publication-boundary.md](ai-publication-boundary.md).
- A removed thing is named in the release notes with what replaces it, or with
  "no replacement" when that is the truth.

The project does not keep a long-term support branch. The latest release is the
supported one and security fixes land in a patch release off master, which is
the commitment in [SECURITY.md](../SECURITY.md). If you must stay on an older
version, you are on your own for fixes, and the changelog is the record of what
you are missing.

## What is not promised

- A release cadence. Releases follow code, not a calendar.
- Support for older versions, backports, or a maintenance branch.
- Compatibility across a major version, or a downgrade path.
- Fixed times for a vendor break. The fix ships when it is right, and the
  response targets are best effort.
