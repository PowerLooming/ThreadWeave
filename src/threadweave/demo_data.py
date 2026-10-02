# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Fictional demo dataset — a populated palace with no tenant required.

`threadweave demo` seeds this into a throwaway SQLite database so a new
user (or a reviewer) can see the capture → search → answer loop without
connecting Microsoft 365, Google Workspace, or MemPalace.

Everything here is invented. Names match the examples on
threadweave.net so the docs, the site and the demo tell one story.
No real person, company, tenant or customer appears in this file.
"""

from __future__ import annotations

DEMO_TENANT = "demo"

# Authors used across the dataset (fictional, and the same names the
# landing page uses in its examples).
AUTHORS = {
    "lars": "lars.johansen",
    "maria": "maria.chen",
    "erik": "erik.solberg",
    "priya": "priya.nair",
    "tom": "tom.bakken",
}

WINGS = ["engineering", "sales", "support", "operations"]


def _e(
    entry_id: str,
    wing: str,
    room: str,
    author: str,
    created_at: str,
    content_type: str,
    source_type: str,
    title: str,
    content: str,
    *,
    entities: list[str] | None = None,
    sensitivity: str = "internal",
    metadata: dict | None = None,
    version_of: str | None = None,
) -> dict:
    """Build one demo entry in the shape EntryStore persists."""
    return {
        "id": entry_id,
        "content": content,
        "content_en": "",
        "wing": wing,
        "room": room,
        "scope": "team",
        "source_type": source_type,
        "author_id": AUTHORS[author],
        "title": title,
        "created_at": created_at,
        "entities": entities or [],
        "content_type": content_type,
        "has_pii": False,
        "tenant_id": DEMO_TENANT,
        "source_metadata": metadata or {},
        "sensitivity": sensitivity,
        "allowed_people": [],
        "acl": {},
        "refinement_notes": [],
        "version_of": version_of,
    }


def demo_entries() -> list[dict]:
    """Return the full fictional palace (ordered oldest first)."""
    entries: list[dict] = []

    # ── engineering ───────────────────────────────────────────────
    entries.append(_e(
        "demo-eng-001", "engineering", "database", "lars",
        "2024-06-12T09:14:00+00:00", "decision", "email",
        "Decision: Postgres over MySQL for v2",
        "We decided to go with Postgres. The main reason is JSONB support: we "
        "need to store semi-structured config data alongside relational records "
        "and MySQL's JSON implementation was still lagging on indexing. "
        "Decision made in the v2 architecture review.",
        entities=["postgres", "mysql", "jsonb"],
        metadata={"thread": "Database evaluation for v2", "message_id": "<demo-1>"},
    ))
    entries.append(_e(
        "demo-eng-002", "engineering", "database", "maria",
        "2024-08-03T11:02:00+00:00", "answer", "gws-chat",
        "Postgres won again for the analytics pipeline",
        "We also evaluated Postgres vs MySQL for the real-time analytics "
        "pipeline. Postgres won again: its COPY command and parallel query "
        "execution gave us 3x throughput on bulk ingestion. The team felt "
        "strongly about standardizing on one database.",
        entities=["postgres", "copy", "analytics"],
        metadata={"space": "#platform-engineering"},
    ))
    entries.append(_e(
        "demo-eng-003", "engineering", "database", "erik",
        "2025-01-18T16:40:00+00:00", "decision", "teams",
        "Post-mortem: MySQL 8.0 upgrade outage",
        "Post-mortem on the MySQL 8.0 upgrade that caused a four-hour outage: "
        "the new default authentication plugin broke our client config. We "
        "documented the fix and committed to moving the remaining MySQL "
        "instances to Postgres by Q3. No more dual-stack.",
        entities=["mysql", "postgres", "outage"],
        metadata={"channel": "MySQL upgrade post-mortem"},
    ))
    entries.append(_e(
        "demo-eng-004", "engineering", "deployment", "lars",
        "2024-04-02T08:05:00+00:00", "answer", "email",
        "Never deploy on a Friday without a rollback owner",
        "Rule we agreed on after the March incident: any Friday deploy needs a "
        "named rollback owner on call until Monday morning. No rollback owner, "
        "no deploy. The on-call rotation is the source of truth for who that is.",
        entities=["deployment", "on-call"],
    ))
    entries.append(_e(
        "demo-eng-005", "engineering", "deployment", "priya",
        "2024-11-21T13:22:00+00:00", "answer", "email",
        "Integration tests must pass before prod, no exceptions",
        "Always run the integration suite before deploying to prod. Twice now "
        "we skipped it for a 'trivial' config change and both times it was the "
        "config that broke. The pipeline enforces it; do not bypass the gate.",
        entities=["ci", "testing"],
    ))
    entries.append(_e(
        "demo-eng-006", "engineering", "deployment", "tom",
        "2025-03-09T07:48:00+00:00", "reference", "drive",
        "Runbook: rollback a bad release",
        "Rollback runbook. Step 1: freeze the pipeline. Step 2: identify the "
        "last green tag. Step 3: redeploy that tag, do not revert the commit. "
        "Step 4: after recovery, post the timeline in the incident channel.",
        entities=["rollback", "runbook"],
        metadata={"source_file": "runbooks/rollback.md"},
    ))
    entries.append(_e(
        "demo-eng-007", "engineering", "deployment", "tom",
        "2025-06-30T10:15:00+00:00", "reference", "drive",
        "Runbook: rollback a bad release (v2)",
        "Rollback runbook, v2. Step 1: freeze the pipeline. Step 2: find the "
        "last green tag in the release list. Step 3: redeploy that tag, never "
        "revert the commit. Step 4: post the timeline in the incident channel. "
        "Step 5 (new): if the bad release touched the schema, page the DBA "
        "before redeploying.",
        entities=["rollback", "runbook", "schema"],
        metadata={"source_file": "runbooks/rollback.md"},
        version_of="demo-eng-006",
    ))
    entries.append(_e(
        "demo-eng-008", "engineering", "architecture", "maria",
        "2024-09-14T15:30:00+00:00", "decision", "email",
        "Decision: connection pooling is mandatory",
        "Every service connects through a pool. Direct connections from "
        "short-lived jobs exhausted the server twice. Pool size stays under the "
        "server's max_connections divided by the number of services.",
        entities=["connection-pooling"],
    ))
    entries.append(_e(
        "demo-eng-009", "engineering", "architecture", "erik",
        "2025-02-27T12:10:00+00:00", "answer", "teams",
        "Why we chose one-way data flows",
        "Anything that reads from a customer system writes into our own store "
        "and stops there. No round trips, no callbacks into their network. This "
        "came out of the security review: a bidirectional sync needs inbound "
        "firewall rules their admins will not approve.",
        entities=["architecture", "security"],
    ))
    entries.append(_e(
        "demo-eng-010", "engineering", "security", "priya",
        "2025-04-08T09:55:00+00:00", "decision", "email",
        "Decision: secrets never in environment files committed to git",
        "Secrets live in per-daemon env files outside the repo or in the secret "
        "manager. A committed .env with a live key means rotating the key, not "
        "just deleting the file. Assume anything pushed is public.",
        entities=["secrets", "git"],
        sensitivity="confidential",
    ))
    entries.append(_e(
        "demo-eng-011", "engineering", "incidents", "erik",
        "2025-05-19T03:12:00+00:00", "answer", "teams",
        "Incident: search returned nothing for 40 minutes",
        "Search went empty for 40 minutes. Cause: the index process held the "
        "port and the service silently degraded instead of failing loudly. Fix: "
        "health check now fails fast when the index is unreachable, and the "
        "startup log names the process holding the port.",
        entities=["search", "port", "incident"],
    ))
    entries.append(_e(
        "demo-eng-012", "engineering", "incidents", "lars",
        "2024-07-25T20:44:00+00:00", "answer", "email",
        "Incident: nightly job hammered the mail API",
        "The nightly job hit the mail API hard enough to get the service "
        "throttled for the whole tenant. Cause: an orphaned worker survived a "
        "restart and a second scheduler started on top of it. Fix: single-writer "
        "lock file, and check for an existing listener before starting.",
        entities=["throttling", "scheduler"],
    ))

    # ── sales ─────────────────────────────────────────────────────
    entries.append(_e(
        "demo-sales-001", "sales", "pricing", "maria",
        "2024-05-08T10:00:00+00:00", "decision", "email",
        "Decision: discount floor is 15 percent",
        "The discount floor stays at 15 percent without approval from the "
        "regional lead. Below that the deal needs a written margin note. This "
        "exists because three deals last quarter landed under 20 percent "
        "margin after a verbal promise nobody could trace.",
        entities=["pricing", "discount"],
        sensitivity="confidential",
    ))
    entries.append(_e(
        "demo-sales-002", "sales", "pricing", "tom",
        "2024-10-30T14:20:00+00:00", "answer", "gws-chat",
        "How we quote multi-year deals",
        "Multi-year quotes show the annual price, not the total. Customers "
        "compare annual numbers, and the total makes the first year look "
        "frightening. Include the uplift cap in the same sentence: it is the "
        "second question every time.",
        entities=["pricing", "quotes"],
        sensitivity="confidential",
    ))
    entries.append(_e(
        "demo-sales-003", "sales", "renewals", "priya",
        "2025-01-30T08:35:00+00:00", "answer", "email",
        "Start renewal conversations 120 days out",
        "Renewals start 120 days before expiry, not 30. At 30 days the customer "
        "has already budgeted for the year and we have no room to move. The "
        "handover from the account team to renewals happens at 120 days with "
        "the usage summary attached.",
        entities=["renewals"],
    ))
    entries.append(_e(
        "demo-sales-004", "sales", "onboarding", "lars",
        "2024-08-27T09:10:00+00:00", "reference", "drive",
        "New customer kickoff checklist",
        "Kickoff checklist: confirm the technical contact, agree the first "
        "milestone and its date, collect the data sources, set the weekly "
        "check-in, and write down who signs off on success. Miss the last one "
        "and the project has no ending.",
        entities=["onboarding", "checklist"],
        metadata={"source_file": "sales/kickoff-checklist.md"},
    ))
    entries.append(_e(
        "demo-sales-005", "sales", "onboarding", "erik",
        "2025-06-04T11:05:00+00:00", "answer", "teams",
        "Pilot customers need one named owner on their side",
        "A pilot without one named owner on the customer side stalls in week "
        "three. Ask for the owner in the kickoff and put their name in the "
        "project notes. If they cannot name one, that is the finding.",
        entities=["pilot", "onboarding"],
    ))
    entries.append(_e(
        "demo-sales-006", "sales", "competitors", "maria",
        "2025-05-12T16:00:00+00:00", "reference", "drive",
        "When a customer asks why not a cloud product",
        "Answer the hosting question first, not the feature question. Most "
        "buyers who raise it have a data processing agreement they cannot "
        "change. Then talk about who can read the data, and only then about "
        "features.",
        entities=["hosting", "cloud"],
        sensitivity="confidential",
    ))

    # ── support ───────────────────────────────────────────────────
    entries.append(_e(
        "demo-support-001", "support", "troubleshooting", "tom",
        "2024-03-19T07:25:00+00:00", "answer", "email",
        "Login loops are usually a clock skew issue",
        "A user stuck in a login loop is nearly always a clock more than three "
        "minutes off. Check the device time before touching the account. Second "
        "cause is a stale cached token: clear the application cache and retry.",
        entities=["login", "clock-skew"],
    ))
    entries.append(_e(
        "demo-support-002", "support", "troubleshooting", "priya",
        "2024-12-11T13:40:00+00:00", "answer", "gws-chat",
        "Missing permissions look like missing data",
        "When a customer says data is missing, check their group membership "
        "before checking the pipeline. Four of the last six of these were a "
        "group that was never added to the folder, not a failed import.",
        entities=["permissions", "groups"],
    ))
    entries.append(_e(
        "demo-support-003", "support", "escalation", "lars",
        "2025-02-14T10:50:00+00:00", "decision", "email",
        "Escalate to engineering after two failed attempts, not three",
        "Support escalates after two attempts at the same symptom. The third "
        "attempt has never once solved it, and it delays the fix by a day. "
        "Include the timestamps and the customer's exact words in the ticket.",
        entities=["escalation"],
    ))
    entries.append(_e(
        "demo-support-004", "support", "escalation", "erik",
        "2025-07-02T15:15:00+00:00", "answer", "teams",
        "Severity is about the customer's work, not our effort",
        "Severity follows the customer's work stoppage, not how hard the fix "
        "looks. A cosmetic bug on a page they use every hour outranks a tricky "
        "bug in a feature they check monthly.",
        entities=["severity", "sla"],
    ))
    entries.append(_e(
        "demo-support-005", "support", "customers", "maria",
        "2025-08-20T09:30:00+00:00", "answer", "email",
        "Customer keeps their own export of everything",
        "One customer exports their entire dataset every month and keeps it "
        "offline. Nothing we do with retention settings reaches that copy. "
        "Treat any deletion request as applying to our side only and say so "
        "plainly.",
        entities=["retention", "export"],
        sensitivity="client_confidential",
    ))

    # ── operations ────────────────────────────────────────────────
    entries.append(_e(
        "demo-ops-001", "operations", "facilities", "tom",
        "2024-02-06T08:00:00+00:00", "reference", "drive",
        "Building access after hours",
        "After-hours access needs a ticket the day before, not an email the "
        "same evening. The night guard works from the ticket list only and "
        "cannot verify a message from a manager.",
        entities=["access", "facilities"],
        sensitivity="internal",
    ))
    entries.append(_e(
        "demo-ops-002", "operations", "procurement", "priya",
        "2024-09-05T11:45:00+00:00", "answer", "email",
        "Anything over 5000 needs two quotes",
        "Purchases over 5000 need two quotes before approval, even from the "
        "existing supplier. The second quote takes a day and it has paid for "
        "itself several times.",
        entities=["procurement", "quotes"],
        sensitivity="confidential",
    ))
    entries.append(_e(
        "demo-ops-003", "operations", "hr", "maria",
        "2025-01-09T14:00:00+00:00", "answer", "teams",
        "Handover when someone leaves: three weeks, not the last three days",
        "Knowledge handover runs the last three weeks, in writing, into the "
        "shared store. The exit interview is not a handover. Sessions get "
        "recorded in the store, not in a private notebook.",
        entities=["offboarding", "handover"],
        sensitivity="hr_privileged",
    ))
    entries.append(_e(
        "demo-ops-004", "operations", "hr", "erik",
        "2025-03-21T10:20:00+00:00", "answer", "email",
        "Salary data never leaves the HR room",
        "Compensation details stay in the HR scope and are never written to a "
        "team room, even in aggregate with small counts. Three people is enough "
        "to identify a salary and someone will try.",
        entities=["compensation", "privacy"],
        sensitivity="hr_privileged",
    ))
    entries.append(_e(
        "demo-ops-005", "operations", "vendors", "lars",
        "2025-04-29T09:00:00+00:00", "decision", "email",
        "Decision: no vendor gets standing network access",
        "Vendors get time-boxed access with an expiry date in the ticket. "
        "Standing access for a support contract outlives the contract and "
        "nobody remembers to remove it.",
        entities=["vendors", "access"],
        sensitivity="confidential",
    ))
    entries.append(_e(
        "demo-ops-006", "operations", "vendors", "tom",
        "2025-08-11T13:05:00+00:00", "answer", "gws-chat",
        "Vendor reviews happen before renewal, not after",
        "The security review happens before the renewal date, not in the quiet "
        "weeks after. Reviewing after renewal means we have already lost the "
        "leverage to change anything.",
        entities=["vendors", "renewal"],
    ))

    return entries


def demo_summary(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Group the demo palace by wing → room → entry count (for the CLI)."""
    summary: dict[str, dict[str, int]] = {}
    for entry in entries:
        wing = entry.get("wing", "")
        room = entry.get("room", "")
        summary.setdefault(wing, {})
        summary[wing][room] = summary[wing].get(room, 0) + 1
    return summary
