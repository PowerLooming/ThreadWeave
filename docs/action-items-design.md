# Assigned Tasks (Action Items) — Design

Status: Draft for review
Author: Hermes Agent + Harald Daltveit
Scope: ThreadWeave core + Teams bot surface

## 1. Overview

ThreadWeave today captures *knowledge*: decisions, answers, and expertise
that passively flows through email, Teams, SharePoint, and Google Workspace.
This proposal adds a second, complementary signal: **assigned tasks** — the
action items embedded in the same content ("Harald, can you chase the vendor
by Friday", "someone needs to own the QA run").

These assignments are usually the *most* time-sensitive thing in a message
and the easiest to lose, yet today the detector ignores them entirely. The
goal is not to build a Planner replacement. It is to surface, per person,
the commitments that were made in the course of ordinary work, in the place
where the work already happens.

### Why this is different from a task manager

| | Planner / To Do | ThreadWeave action items |
|---|---|---|
| Source | A user actively creates a task | Tasks are *found* inside captured content |
| Completeness | Only what people bothered to enter | Whatever was actually said or written |
| Owner | Explicit assignment field | Inferred from responsibility language |
| Value | A to-do list | A memory of commitments, searchable and auditable |

The differentiator is capture. The tasks you forgot to enter are exactly the
ones this feature finds.

## 2. What counts as an "assigned task"

An action item has three parts. It is only worth recording when we can
confidently extract at least the first two:

1. **Owner** — a specific person ("Harald", "you" referring to the author,
   "the QA team" as a named group). Unresolvable owners ("someone should",
   "we need to") are a weak signal and should not produce a task entry.
2. **Action** — the thing to be done ("chase the vendor", "own the QA run",
   "follow up with IT").
3. **Context** — optional: deadline ("by Friday"), a ticket/PR reference,
   or a linked conversation. Retained for the notification and the entry
   metadata, not required for capture.

### Signal language (responsibility patterns)

The detector already has pattern families for decisions and answers. We add a
parallel family for *responsibility assignment*:

- Direct request to a person: `can you`, `could you`, `would you`, `will you`
  (+ action verb)
- Delegation / handoff: `can (you/NAME) take`, `let (NAME) handle`,
  `could you follow up on`, `are you on it`
- Ownership statements: `you need to`, `you should`, `someone should`,
  `someone needs to own`, `the team needs to`, `who's on`
- Follow-up obligations: `please follow up`, `don't forget to`,
  `remember to`, `follow through on`, `get back to (NAME) about`
- Passive responsibility (weaker): `this is on (NAME)`, `(NAME) is
  responsible for`, `(NAME) owns`

### Owner resolution

A detected action only becomes a *task* when its owner resolves to a known
identity. Resolution order:

1. **Direct second person** ("you") → the *author of the message* (available
   in email sender / Teams from). This is the most common and most reliable
   case: someone telling you to do something is a task assigned to you.
2. **Explicit name** ("Harald", "Adele") → resolve against the org model
   (`OrgModel` person entities) and/or an org directory list. Unresolved
   names get a `person_unresolved` flag and are held back from listing.
3. **Named team** ("the QA team") → resolve to a team entity; only listed for
   members of that team.

### What is NOT a task

- **Questions** (already excluded by the QUESTION class): "can you check the
  docs?" is a request for info, not an ongoing obligation.
- **Statements of fact** ("the vendor is chasing us") — no imperative.
- **Generic/ambiguous** ("someone should fix this") with no resolvable owner.
- **Hearsay / gossip** (already rejected by `has_gossip`).
- **External/marketing** (already rejected by `EXTERNAL_SOURCE_PATTERNS`).

## 3. Detection design

### New class vs. new flag

`ContentType` is a single-label enum (`ANSWER`, `DECISION`, `QUESTION`,
`CHAT`, `REFERENCE`). An action item is *not* mutually exclusive with those —
a message can be a decision *and* carry an assignment. So we do **not** add a
new ContentType (which would force a single label). Instead we add a
**side-channel flag** on `DetectionResult`:

```python
@dataclass
class DetectionResult:
    ...
    action_items: list[ActionItem] = field(default_factory=list)

@dataclass
class ActionItem:
    owner: str            # resolved person id, or "" if unresolved
    owner_name: str       # display name / raw mention
    owner_resolved: bool  # False if we could not map to a known person
    action: str           # the verb phrase / thing to do
    deadline: str = ""    # ISO date if a deadline was parsed
    confidence: float
    source_text: str      # the sentence(s) the action came from
```

- The existing classification (`is_worth_saving`) is unchanged.
- `detect()` additionally scans for `action_items` and populates the flag.
- A message that is only an action item (no answer/decision confidence) is
  still a weak save candidate on its own, but the action item itself is
  retained in `source_metadata` for a later pass (see §4).

This keeps the change additive: every existing caller keeps working, and the
new field is optional.

### Two-engine behavior

- **Regex path**: the `ACTION_PATTERNS` family above. Deterministic, cheap,
  works offline. Resolves owners via the same `_extract_entities` + org model.
- **LLM path** (`llm_detector.py`): prompt already returns structured JSON for
  classification. Extend the prompt to also emit `action_items` with
  `owner`, `action`, `deadline`. This gets multilingual assignment detection
  for free (the existing multilingual detector covers CJK and other scripts),
  and it is better at resolving pronouns like "you" and "we" to the right
  person.

Owner resolution for the "you" case needs the message author context, which
`detect(text)` does not have today. So the action-item scan should be a
separate function that receives `author_id`:

```python
def extract_action_items(
    text: str, author_id: str, org: OrgModel | None = None,
) -> list[ActionItem]
```

The connectors (email processor, Teams bot, SharePoint processor) already
know the author; they pass it in. `detect()` keeps its signature; callers
that want action items call `extract_action_items` alongside.

## 4. Data model & storage

Design principle from the existing system: **tasks are entries**. Storing an
action item as a normal knowledge entry gets it the full existing pipeline
for free — MemPalace hybrid search, confidentiality layers, PII/gossip gates,
opt-out registry, and versioning. No new database.

### Fields

The `EntryStore` schema is already flexible via `source_metadata` (a JSON
column) and `entities` (a JSON column). Action items fit cleanly:

- `content` → the action sentence (e.g. "Harald to chase the vendor by
  Friday").
- `content_type` → keep the primary type (decision/answer); do **not** store
  `task` here to avoid breaking the existing save/ignore logic.
- `source_metadata["action_item"]` → `true` when the entry was captured
  primarily as an assignment.
- `source_metadata["action_owner"]` → resolved person id.
- `source_metadata["action_owner_name"]` → display name.
- `source_metadata["action_deadline"]` → ISO date when parsed.
- `source_metadata["action_status"]` → `open` | `suggested_done` | `done`
  (see §6).
- `entities` → include `{"type": "person", "value": "<owner>"}` so action
  items surface in people-centric search.

### Optional dedicated index (later phase)

A lightweight derived table `action_items` (owner, action, deadline, status,
entry_id, created_at, done_at) is worth adding only when we need efficient
per-person "what's open" queries at scale. For the pilot, filtering
`source_metadata.action_item = true` + `action_owner = <person>` over the
existing entry store is sufficient. We add the index when the query profile
justifies it — done-right-first-time, but only the abstraction is built now,
the materialized store comes with real demand.

## 5. Person resolution & org model linkage

`OrgModel` already stores people, teams, and reporting lines as temporal
triples. Action-item owners resolve against it:

- `OrgModel` provides a `resolve_person(name)` that matches display names,
  email local-parts, and AAD ids.
- Email/Teams authors are already identified (`author_id` = email or AAD id);
  those map directly to person entities.
- For a **named** owner ("Harald"), `resolve_person` returns a match or
  `None`. A `None` sets `owner_resolved = False`, and the item is held back
  from *listing* but still searchable (so it is not lost).

Directory bootstrap: reuse whatever the org model already ingests (Teams
roster, GWS directory, HR feed). No new ingestion in this feature.

## 6. Status lifecycle

We deliberately do **not** build a full task workflow, but status has to be
honest about *when we know* a task is done. Two kinds of evidence exist:

1. **Explicit**: the owner (or bot admin) marks it done via `tasks done <n>`.
2. **Detected**: a captured message says the work is complete ("I've chased
   the vendor", "QA run is done", "resolved the X ticket").

So status is a small enum, not a single boolean:

```python
class ActionStatus(str, Enum):
    OPEN = "open"             # assigned, not yet known to be done
    DONE = "done"             # explicitly marked done (or confirmed)
    SUGGESTED_DONE = "suggested_done"  # auto-detected, awaiting confirmation
```

- New action items start `OPEN`.
- `DONE` is set by an explicit `tasks done <n>` / CLI, **or** by confirming a
  detected completion (below).
- `SUGGESTED_DONE` is set by the detection engine when it *thinks* the work is
  complete but has not been confirmed.

### Completion detection — the correlation problem

Assignment detection *finds* a task; completion detection must *link a "done"
statement back to a specific open task*. That correlation is the hard part,
and a wrong link silently erases a real commitment. Rules to keep it
high-precision:

- **Detect the completion signal**: past-tense / result language tied to the
  assigned action — "I've/we've done", "chased", "followed up", "resolved",
  "finished", "closed", "wrapped up", "handled", "completed", plus the same
  verb the assignment used. The detector's existing past-tense decision
  patterns (`resolved|implemented|merged|fixed|patched|completed|finished`)
  are a starting point, but they must be scoped to the assigned action, not
  any past-tense verb.
- **Correlate to an open task** for the same owner by matching the action verb
  and, where present, the same object / ticket / deadline window. A match is
  only accepted when the owner matches **and** the action overlaps.
- **The match must never be silent.** Auto-detected completion sets
  `SUGGESTED_DONE`, never `DONE`. The bot DMs the owner: "Looks like 'chase
  the vendor' is done? Say 'tasks done 1' to confirm, or ignore." The task is
  only closed on confirmation. This preserves the audit trail and prevents a
  false positive from erasing a real commitment.
- **No match → no status change.** A "done" statement with no correlating
  open task changes nothing. It stays open.

### Status transitions

```
OPEN ──tasks done──▶ DONE
OPEN ──detected──▶ SUGGESTED_DONE ──tasks done──▶ DONE
                      │
                      └──(no confirm)──▶ OPEN (on explicit "not done" or timeout)
```

- Closed and suggested-done items remain searchable (audit trail: "we said
  we would, and did we"), but default "what's open" queries show only `OPEN`.
  `SUGGESTED_DONE` is shown separately ("pending confirmation"), not counted
  as open or done.

This keeps the feature truthful: ThreadWeave *records* commitments and
*proposes* completions, but it never silently decides on its own.

## 7. Bot / CLI surface

The Teams bot already has a command layer (`_handle_privacy_command` with
opt-out/opt-in/delete/search/status) and a DM conversation store. Add a
parallel "tasks" command group, available via @mention or 1:1 DM:

- `@ThreadWeave my tasks` → list the caller's **open** action items (newest
  first, deadline shown if present).
- `@ThreadWeave tasks for <name>` → list open action items assigned to a
  named person (respects that person's opt-out — see §9).
- `@ThreadWeave tasks done <n>` → mark the nth listed item as done (closes
  an `OPEN` or confirms a `SUGGESTED_DONE`).
- `@ThreadWeave tasks not done <n>` → reject a suggested completion, back to
  `OPEN`.
- `@ThreadWeave tasks all` → open items across the team (admin/moderation
  use; gated by scope and confidentiality).
- `@ThreadWeave tasks search <query>` → search action-item entries.

CLI equivalents:

- `threadweave tasks list [--owner <name>] [--open|--all|--pending]`
- `threadweave tasks done <entry_id>`
- `threadweave tasks undone <entry_id>`

The notification poller (already used for "camera sign" capture DMs) can
optionally DM an author when a message *they were assigned to* is captured:
"Noted: Harald to chase the vendor by Friday. Say 'tasks done 1' when done."
This is the natural fit with the existing passive capture flow, and it is
the feature's clearest immediate value — the assignment surfaces at the
moment of capture, not days later.

## 8. Manager view — `tasks for my team`

A manager can get an overview of the open tasks assigned to the people who
report to them. This is a distinct scope from the global `tasks all`:

| Scope | Who sees it | Gating |
|---|---|---|
| `my tasks` | The caller's own open tasks | Caller only |
| `tasks for <name>` | A named person's open tasks | Person's opt-out + scope |
| `tasks for my team` | Open tasks of the caller's reports | `reports_to` chain + opt-out + scope |
| `tasks all` | Every open task in the tenant | Admin/moderation only |

### How it resolves

`OrgModel` already stores `reports_to` as temporal triples, so "who reports
to whom" is queryable as of a point in time with no new data model. The
bot resolves the caller, finds their reports via `reports_to`, and lists the
open tasks assigned to those people.

Depth is a config choice:
- **Direct reports only** (one level) — simplest, least surprising.
- **Whole reporting line** (direct reports plus their reports, recursively)
  — broader, useful for org-wide managers.

Default to **direct reports only** in v1; the recursive option is a config
flag flipped only with demand.

### The pre-existing-relationship edge case

Decide explicitly whether a manager sees tasks assigned to a report *before*
that person came under them. The org model's validity windows support either
answer:

- **Current-relationship only (recommended):** show only tasks assigned
  while the `reports_to` relationship was active. This avoids a new manager
  being held responsible for commitments made under a previous manager.
- **All open tasks:** show every open task the report has, regardless of
  when it was assigned.

Recommended: **current-relationship only**, using the relationship `valid_from`
as the lower bound. A manager sees the current state of their team, not
history that predates their authority.

### Confidentiality carries over unchanged

The §9 confidentiality rules apply exactly as for the individual view:

- **The report's opt-out always wins.** If a report has opted out, the
  manager does not see their tasks, even though the manager outranks them.
  A manager cannot use the team view to bypass a person's consent.
- **Scope is respected.** Tasks marked `internal` or with a `sensitivity`
  that excludes the manager stay hidden. The manager view never crosses a
  confidentiality boundary the task itself set.
- **Reporting line only.** The manager sees their own reports, never
  arbitrary people. That is what separates it from the admin `tasks all`.

### Bot / CLI surface

- `@ThreadWeave tasks for my team` → open tasks of the caller's direct
  reports (respecting each report's opt-out and scope).
- `@ThreadWeave tasks for my team --all-levels` → recursive reporting line.
- `threadweave tasks team [--owner <manager>]` → CLI equivalent.

The manager view is **not** admin-only, because it is bounded by the caller's
own reporting line. It is only that line, and it respects every existing
consent and confidentiality rule.

## 9. Confidentiality, PII, gossip, opt-out

Because action items ride the existing entry pipeline, every existing gate
applies. Two points need explicit care:

1. **The owner's opt-out matters.** If the person a task is *assigned to* has
   opted out, the assignment is about them and must not be listed or
   notified to them. Rule: an action item assigned to person X is only
   surfaced to X if X is opted in, and only shown to others according to the
   entry's scope/confidentiality. Respect `OptOutRegistry` on both the owner
   and the author before saving/listing.
2. **Assignee-as-PII.** "Adele must update the salary file" names a person in
   a context that may be sensitive. The action-item scan should run *after*
   the PII gate and should not lower the bar: a task that would be rejected
   as PII/gossip is not rescued by being an action item. A mention of a
   person is not itself PII, but a task touching a PII-gated subject
   (compensation, health, home address) inherits the rejection.

The `sensitivity` field is preserved on the entry; a task inside
`internal`/confidential content is subject to the same scope rules as any
other entry. Listing "my tasks" never crosses the owner's confidentiality
boundary.

## 10. Edge cases & pitfalls

- **Pronoun ambiguity.** "We should fix X" has no single owner → no task.
  Only a resolvable owner produces an entry. Conservative is correct.
- **Polite phrasing is not an assignment.** "Would you be able to look at
  this sometime?" is soft. Require an action verb, not just a polite opener.
- **"you" in instructions vs. assignments.** Documentation ("you need to
  set the flag") is an answer, not a task for the author. Distinguish by
  presence of the imperative + an actionable verb and a concrete object.
  When in doubt, the LLM path classifies; the regex path stays conservative.
- **Deadlines.** Parse only explicit dates ("by Friday", "before 2026-09-01",
  "by end of week") and never guess. Store the raw string alongside a parsed
  ISO date when available.
- **Duplicate capture.** The same assignment may appear in an email and a
  Teams follow-up. `find_by_source_key` already dedupes by source identity;
  rely on it rather than content comparison.
- **Teams "you" is the sender, not the bot.** Resolution must use the message
  author identity, never the bot's own identity.
- **Group chats / multiple "you".** "You all need to..." → no single owner →
  no task (or resolve to the named team if present).
- **Ordering with existing detection.** Action items must not inflate
  ANSWER/DECISION confidence or force a save. They are a side-channel; the
  existing `is_worth_saving` threshold is untouched.

## 11. Testing plan

- **Unit (detector):** a fixture of sample assignments — direct "can you",
  delegation to a named person, ownership statements, soft/polite requests
  (must NOT detect), ambiguous "someone should" (unresolved), "you" as
  instruction vs. task, deadline parsing, multilingual samples via the LLM
  path.
- **Unit (owner resolution):** resolve "you" → author; resolve named owner →
  org model; unresolved name → `owner_resolved=False`.
- **Unit (store):** action-item fields round-trip through `_to_row` /
  `_from_row`; `source_metadata` survives; status transitions
  open→suggested_done→done persist and round-trip.
- **Completion detection:** past-tense completion signal tied to an assigned
  action is correlated to the same-owner open task; a "done" statement with
  no matching open task changes nothing; a false-positive match sets
  `SUGGESTED_DONE` and never `DONE` without confirmation.
- **Gate tests:** task touching a PII subject is rejected; gossip content
  with an assignment is rejected; owner opted out → item not listed/notified.
- **Manager view:** `tasks for my team` lists only direct reports' open
  tasks; a report who is opted out is excluded even for their manager;
  a task predating the `reports_to` relationship is hidden (current-
  relationship default); `--all-levels` recurses.
- **Bot/CLI:** `my tasks`, `tasks for <name>`, `tasks for my team`,
  `tasks done <n>`, `tasks all`, `tasks search`. DM vs channel mention
  paths. Empty-state handling.
- **Integration:** capture an assignment from a simulated Teams message and
  assert the entry lands with correct metadata and appears in `tasks list`.

Reuse the existing conftest isolation conventions (dedicated
`THREADWEAVE_*_DB` per test) so tests never touch the live store.

## 12. Rollout phases

**Phase 1 — Capture (core).** `ActionItem` dataclass, `extract_action_items`
regex engine, owner resolution, entry storage via `source_metadata`, CLI
`tasks list`/`tasks done`. Tests as above.

**Phase 2 — Bot surface.** Teams `tasks` command group + notification DM at
capture time ("Noted: ... say 'tasks done 1' when done"). Pilot with a real
user (Harald) to calibrate precision of the responsibility patterns.

**Phase 3 — Completion detection.** Correlate "done" statements to open
tasks, set `SUGGESTED_DONE`, and DM the owner for confirmation. `tasks done`
confirms, `tasks not done` rejects. Highest-precision signals only.

**Phase 4 — Multilingual + LLM.** Extend `llm_detector` prompt to emit
`action_items`; verify across languages.

**Phase 5 — Optional index.** Materialized `action_items` table only when
per-person "what's open" queries need it (scale-driven).

## 13. Decisions (confirmed)

Review feedback folded in. These are locked for the build:

1. **Unresolved-owner items** ("someone should fix X") are **saved but not
   listed**. They remain fully searchable so nothing is lost, but they never
   appear in a person's open-tasks list (there is no one to own them).
   They surface only via `tasks search` and as `owner_resolved=false`
   entries. Rationale: losing a real commitment is worse than holding a
   vaguer one searchable-but-hidden.
2. **Completion detection is in, but confirmation-gated.** Status is a
   three-state enum (`open` / `suggested_done` / `done`), not a boolean.
   Auto-detected completion sets `SUGGESTED_DONE`, never `DONE`; the bot DMs
   the owner to confirm (`tasks done`) or reject (`tasks not done`). A
   false-positive suggestion never silently erases a commitment, and a
   "done" statement with no matching open task changes nothing. This keeps
   the value of automation without the risk of a wrong auto-close.
3. **Capture-time DM is high-confidence only, and gated by the author's
   opt-in.** A "Noted: ... say 'tasks done 1' when done" DM fires only when
   the assignment is high-confidence (resolved owner + action verb) **and**
   the author is opted in. Low-confidence or unresolved items never trigger
   a notification, to avoid fatigue and false alarms.
4. **`tasks all` (cross-person) is admin/moderation-only in v1.** Normal
   users get `my tasks`, `tasks for <name>` (respecting that person's
   opt-out and scope), `tasks done`, and `tasks search`. Cross-person
   listing is gated by role so it cannot be used to surveil assignments
   outside the caller's confidentiality boundary.

These fold into the Phase 1 acceptance criteria: capture → resolve → store
→ list → close, with opt-out and confidentiality enforced end to end.

