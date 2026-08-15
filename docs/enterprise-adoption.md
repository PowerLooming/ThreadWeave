# Enterprise Adoption Checklist

Tracked gates from the internal IT-manager evaluation (2026-08-08). A
conditional pilot approval was granted for one team; these gates must be
met before wider rollout. Each item has acceptance criteria so progress
is verifiable, not aspirational.

Status legend: `[ ]` open, `[x]` done, `[~]` in progress.

## Gate 1: Least-privilege permission story

**Why:** the app registrations hold tenant-wide `Mail.Read` and
`Sites.Read.All`. The daemons only touch what they are pointed at, but
enterprise security review requires a tighter blast radius.

- [ ] Document the current permission matrix (what each registration
      holds, what each daemon actually touches)
- [ ] Implement a delegated-token option for the email connector
      (device-code, mailbox-scoped) for tenants that require it
- [ ] Add a `--dry-run`-style permission audit command that lists
      granted app permissions from Graph
- [ ] Acceptance: a tenant can run the email daemon with delegated
      credentials and no tenant-wide Mail.Read on any registration

## Gate 2: Copilot / Microsoft Search licensing decision

**Why:** items index correctly but search-frontend surfacing and
Copilot answers require licensing the tenant may not hold. The pilot
must not overpromise the Copilot integration.

- [ ] Write the supported-matrix doc: what works on free/Basic/standard
      vs. Copilot-licensed tenants
- [ ] State the Graph-connector surfacing limitation on the website
      setup guide (prevent sales overpromise)
- [ ] Acceptance: a prospective customer can predict, before install,
      whether search surfacing will work in their tenant

## Gate 3: Vendor readiness

**Why:** single-developer project; enterprise procurement needs a
support channel, security contact, and data-processing posture.

- [ ] Publish a support channel (GitHub Discussions or equivalent) with
      response expectations
- [ ] Add a security contact (SECURITY.md with responsible disclosure)
- [ ] Draft the DPA-ready data-handling statement: what data flows
      where, subprocessors (none), retention defaults
- [ ] Define an upgrade/deprecation policy (what happens when a
      connector API breaks)
- [ ] Acceptance: a procurement team can answer "who do we call, and
      what happens if it breaks" from public docs alone

## Gate 4: Observability and alerting

**Why:** the NOC cannot operate a system it cannot watch. Daemons
currently fail into log files.

- [ ] Expose daemon health as a machine-readable endpoint
      (`/api/v1/health` exists; add per-daemon last-poll timestamps)
- [ ] Add a watchdog script that exits non-zero when any daemon has not
      polled within N intervals (cron/systemd timer friendly)
- [ ] Document log locations and log rotation for all daemons
- [ ] Add a metrics hook (JSON/OpenMetrics endpoint) for SIEM ingestion
- [ ] Acceptance: a monitoring check fails within one poll interval of
      a daemon crash, and the operator can see which daemon and why

## Gate 5: Data lifecycle

**Why:** a system that remembers everything must also prove it can
forget on schedule.

- [ ] Document backup/restore for the palace (SQLite file copy or
      PostgreSQL dump) and rehearse it
- [ ] Implement retention policy: per-tenant or per-source maximum age
      with scheduled purge (audited)
- [ ] Add a bulk-export path (all entries of one author or one wing as
      JSON/CSV) for data-portability requests
- [ ] Add a bulk-delete workflow (supervisory/legal request)
- [ ] Acceptance: restore-from-backup rehearsal passes; a retention
      purge deletes only what it should and logs every deletion

## Gate 6: Passive Teams capture without per-team installs

**Why:** the product promise is passive capture. The Teams bot alone
only sees conversations it is installed into (@mention, DM, or
RSC-consented teams/chats). Any team or chat where nobody installs the
bot is dark, and 1:1 chats are never covered by any bot mechanism.

- [~] Implement a `teams-watch` daemon: Graph app-only polling of
      channel messages via delta queries (`ChannelMessage.Read.All` +
      `Team.ReadBasic.All`), same pull-only pattern as email-watch and
      sharepoint-watch. No webhooks, no per-team installs.
      (Built 2026-08-15: `threadweave teams watch`, prime/backfill
      modes, delta-token state file, daemon registration, 9 tests.
      Pending live verification against the pilot tenant.)
- [~] Camera sign for passively captured authors: notification must
      not depend on the author having talked to the bot (activity-feed
      notification via Graph `TeamsActivity.Send`, email fallback).
      (Built 2026-08-15: personal DMs only for personal conversation
      refs, Graph activity-feed fallback for passive authors, email
      fallback via Graph sendMail for tenants that refuse
      TeamsActivity.Send, retry-then-skip with stats. Pending live
      verification.)
- [ ] Document the RSC consent step (Teams admin center, Manage apps,
      Permissions, Review permissions and consent) in the connector
      docs; add a startup probe that detects consent absence.
- [ ] Acceptance: a channel message in a team where the bot was never
      installed is captured within one poll interval, and its author
      receives a capture notification.

## Pilot conditions (running now)

- [ ] Pilot team's knowledge stays in its own wing
- [ ] No Graph sync during the pilot
- [ ] Capture-quality measurement: decisions found vs. missed, sampled
      weekly from the pilot team

## Rollout blockers (do not pass until closed)

- [ ] Gate 1 permission story closed
- [ ] Gate 5 backup/restore rehearsed
- [ ] Gate 4 alerting live
- [ ] Gate 3 vendor commitments published
- [ ] Gate 2 licensing decision documented

## Origin

Full evaluation in the 2026-08-08 IT-manager review: conditional pilot
approval; rejection triggers were permission story, backup/restore
proof, and vendor posture after 90 days.
