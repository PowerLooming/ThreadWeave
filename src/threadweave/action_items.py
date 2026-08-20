# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Action items — responsibility assignments embedded in captured content.

ThreadWeave captures *knowledge* (decisions, answers, expertise). This
module adds a complementary signal: the action items people assign to each
other in the same content ("Harald, can you chase the vendor by Friday",
"someone needs to own the QA run").

Design (see docs/action-items-design.md):

- An action item only becomes a *task* when its owner resolves to a known
  person. "you" resolves to the message author; a named owner resolves
  against the org model; "someone"/"we" are ambiguous and produce an
  unresolved item (saved but never listed as a to-do).
- The existing ContentType classification is untouched. Action items are a
  side-channel: they never inflate ANSWER/DECISION confidence or force a save.
- Status is a three-state enum (open / suggested_done / done). Detected
  completion only ever sets SUGGESTED_DONE — never DONE without explicit
  confirmation — so a false positive can't silently erase a commitment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ActionStatus(str, Enum):
    """Lifecycle of an action item.

    - OPEN: assigned, not yet known to be done.
    - SUGGESTED_DONE: auto-detected completion, awaiting confirmation.
    - DONE: explicitly marked done (or confirmed).
    """
    OPEN = "open"
    SUGGESTED_DONE = "suggested_done"
    DONE = "done"


@dataclass
class ActionItem:
    """A single responsibility assignment extracted from text.

    Only ``action`` and ``source_text`` are guaranteed non-empty. ``owner``
    is "" when unresolved; ``owner_resolved`` is False in that case.
    """
    owner: str                       # resolved person id, or "" if unresolved
    owner_name: str                  # display name / raw mention
    owner_resolved: bool             # False if we could not map to a person
    action: str                      # the verb phrase / thing to do
    deadline: str = ""               # ISO date if a deadline was parsed
    confidence: float = 0.5
    status: ActionStatus = ActionStatus.OPEN
    source_text: str = ""            # the sentence(s) the action came from

    def to_metadata(self) -> dict:
        """Serialize to source_metadata-compatible dict."""
        return {
            "action_item": True,
            "action_owner": self.owner,
            "action_owner_name": self.owner_name,
            "action_owner_resolved": self.owner_resolved,
            "action_deadline": self.deadline,
            "action_status": self.status.value,
            "action": self.action,
            "action_confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_metadata(md: dict) -> "ActionItem | None":
        """Rehydrate from source_metadata; None if not an action item."""
        if not md.get("action_item"):
            return None
        return ActionItem(
            owner=md.get("action_owner", ""),
            owner_name=md.get("action_owner_name", ""),
            owner_resolved=bool(md.get("action_owner_resolved", False)),
            action=md.get("action", ""),
            deadline=md.get("action_deadline", ""),
            confidence=float(md.get("action_confidence", 0.5)),
            status=ActionStatus(md.get("action_status", "open")),
        )


# ── Responsibility patterns ───────────────────────────────────────
# Each pattern is a tuple: (regex, kind). The regex is searched on the
# lowercased text; the kind tells extract_action_items how to treat a match.

# Direct request to the author ("you"): can/could/would/will you <action>
DIRECT_YOU = r"can|could|would|will|are you able to"
DIRECT_PATTERNS = [
    # "can you X", "could you X" — direct assignment to the author
    (rf"\b(?:{DIRECT_YOU})\s+you\s+(?P<act>[a-z][a-z\s,.;'\-]{{4,}})\b", "direct"),
    # "will you take care of X"
    (r"\bwill\s+you\s+(?:take\s+care\s+of|handle|look\s+into|chase|follow\s+up\s+on)\s+"
     r"(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "direct"),
]

# Delegation / handoff to a named person: "let NAME handle X",
# "NAME to take care of X", "could NAME follow up on X"
NAMED_PATTERNS = [
    (r"\blet\s+(?P<owner>[A-Za-z][a-z]+)\s+(?:handle|take|own|look\s+into|chase|"
     r"follow\s+up\s+on)\s+(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "named"),
    (r"\b(?:can|could|would|will)\s+(?P<owner>[A-Za-z][a-z]+)\s+"
     r"(?:handle|take|own|look\s+into|chase|follow\s+up\s+on|be\s+on)\s+"
     r"(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "named"),
    (r"\b(?P<owner>[A-Za-z][a-z]+)\s+(?:is|are|to|will)\s+(?:on|own(?:ing)?|"
     r"responsible\s+for)\s+(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "named"),
    # "NAME should/has to handle X" — obligation on a named person
    (r"\b(?P<owner>[A-Za-z][a-z]+)\s+(?:should|needs\s+to|has\s+to|must)\s+"
     r"(?:handle|take|own|look\s+into|chase|follow\s+up\s+on|be\s+on)\s+"
     r"(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "named"),
]

# Ownership / follow-up obligations: "you need to X", "please follow up on X"
OBLIGATION_PATTERNS = [
    (r"\byou\s+(?:need\s+to|should|have\s+to|must)\s+(?P<act>[a-z][a-z\s,.;'\-]{4,})\b",
     "obligation"),
    (r"\bplease\s+(?:follow\s+up\s+on|chase|handle|look\s+into|take\s+care\s+of|"
     r"get\s+back\s+to)\s+(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{3,})\b", "obligation"),
]

# Ambiguous: "someone should X", "we need to X", "someone needs to own X".
# These never resolve to an owner → saved but not listed.
AMBIGUOUS_PATTERNS = [
    (r"\bsomeone\s+(?:should|needs\s+to|has\s+to|must)\s+(?P<act>[a-z][a-z\s,.;'\-]{4,})\b",
     "ambiguous"),
    (r"\bwe\s+(?:need\s+to|should|have\s+to|must)\s+(?P<act>[a-z][a-z\s,.;'\-]{4,})\b",
     "ambiguous"),
    (r"\bsomebody\s+(?:should|needs\s+to)\s+(?P<act>[a-z][a-z\s,.;'\-]{4,})\b",
     "ambiguous"),
]

# Past-tense completion signals, used by suggest_done() (Phase 3).
COMPLETION_VERBS = (
    r"done|finished|completed|closed|wrapped\s+up|handled|resolved|"
    r"chased|followed\s+up\s+on|followed\s+through|took\s+care\s+of|"
    r"implemented|fixed|merged|patched"
)
COMPLETION_PATTERNS = [
    (rf"\b(?:i['\u2019]?ve|we['\u2019]?ve|i\s+have|we\s+have|already)\s+"
     rf"(?:{COMPLETION_VERBS})\s+(?P<act>[a-z0-9][a-z0-9\s,.;'\-]{{3,}})\b",
     "completion"),
]

# All responsibility patterns, checked in order.
ACTION_PATTERNS = (
    DIRECT_PATTERNS + NAMED_PATTERNS + OBLIGATION_PATTERNS + AMBIGUOUS_PATTERNS
)

# Multi-language: the regex engine is English-only by design. The LLM
# detector (llm_detector.py) is extended to emit action_items for other
# languages in Phase 4.

# ── Deadline parsing ──────────────────────────────────────────────
# Only explicit dates are parsed; never guessed. Stores both the raw
# string and an ISO date when available.

_DEADLINE_KEYWORDS = {
    "today": 0, "tomorrow": 1, "eod": 0, "eow": 4, "end of day": 0,
    "end of week": 4, "end of month": 27,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_deadline(text: str) -> str:
    """Best-effort ISO deadline from explicit date language, or ''."""
    low = text.lower()
    # Named weekday: "by Friday" → next occurrence
    for day, _idx in _WEEKDAYS.items():
        m = re.search(rf"\bby\s+({day})\b", low)
        if m:
            return _next_weekday_iso(m.group(1))
    for kw, days in _DEADLINE_KEYWORDS.items():
        if re.search(rf"\bby\s+{kw}\b", low) or re.search(
                rf"\bby\s+{kw}\s+", low):
            return _offset_iso(days)
    # ISO date: "by 2026-09-01" or "by 2026/09/01"
    m = re.search(r"\bby\s+(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b", text)
    if m:
        try:
            dt = datetime.strptime(m.group(1).replace("/", "-"), "%Y-%m-%d")
            return dt.date().isoformat()
        except ValueError:
            pass
    return ""


def _offset_iso(days: int) -> str:
    from datetime import timedelta
    return (datetime.now() + timedelta(days=days)).date().isoformat()


def _next_weekday_iso(day: str) -> str:
    from datetime import timedelta
    target = _WEEKDAYS[day]
    today = datetime.now()
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).date().isoformat()


# ── Owner resolution ──────────────────────────────────────────────

# Words that never resolve to a person.
_NON_PERSON_OWNERS = {
    "you", "your", "we", "us", "our", "someone", "somebody", "everyone",
    "anyone", "the", "a", "an", "i", "it",
}


def _clean_action(raw: str) -> str:
    """Normalize a captured action phrase (strip leading/trailing junk)."""
    act = re.sub(r"\s+", " ", raw).strip(" .,;:!?()\"'")
    # Drop trailing dangling prepositions / articles
    act = re.sub(r"\s+(?:to|the|a|an|and|or|for|on|of)$", "", act).strip()
    return act


def _resolve_named_owner(name: str, org) -> tuple[str, str, bool]:
    """Resolve a capitalized name against the org model.

    Returns (owner_id, display_name, resolved).
    """
    if org is None:
        return "", name, False
    try:
        return org.resolve_person(name)
    except Exception:
        return "", name, False


def extract_action_items(
    text: str,
    author_id: str = "",
    org=None,
    author_name: str = "",
) -> list[ActionItem]:
    """Extract responsibility assignments from text.

    Args:
        text: The content to scan (email body, Teams message, etc.)
        author_id: Identity of the message author ("you" resolves here).
        org: Optional OrgModel for named-owner resolution.
        author_name: Display name of the author, for owner_name on "you".

    Returns:
        List of ActionItem, in the order they appear. Items with an
        ambiguous owner are included with owner_resolved=False.
    """
    items: list[ActionItem] = []
    low = text.lower()

    for pattern, kind in ACTION_PATTERNS:
        for m in re.finditer(pattern, low):
            raw_act = m.groupdict().get("act", "").strip()
            action = _clean_action(raw_act)
            if not action:
                continue
            start = m.start()
            sentence = _surrounding_sentence(text, m.start())

            if kind == "direct":
                # "you" → the author
                items.append(ActionItem(
                    owner=author_id,
                    owner_name=author_name or author_id or "you",
                    owner_resolved=bool(author_id),
                    action=action,
                    deadline=_parse_deadline(sentence),
                    confidence=0.6,
                    source_text=sentence,
                ))
            elif kind == "obligation":
                # "you need to X" → the author (obligation on the reader)
                items.append(ActionItem(
                    owner=author_id,
                    owner_name=author_name or author_id or "you",
                    owner_resolved=bool(author_id),
                    action=action,
                    deadline=_parse_deadline(sentence),
                    confidence=0.55,
                    source_text=sentence,
                ))
            elif kind == "named":
                owner = m.groupdict().get("owner", "")
                # A pronoun here ("can you chase") is a direct assignment,
                # already handled by the direct pattern. Skip non-person owners.
                if owner.lower() in _NON_PERSON_OWNERS:
                    continue
                owner_id, disp, resolved = _resolve_named_owner(owner, org)
                items.append(ActionItem(
                    owner=owner_id,
                    owner_name=disp or owner,
                    owner_resolved=resolved,
                    action=action,
                    deadline=_parse_deadline(sentence),
                    confidence=0.6 if resolved else 0.4,
                    source_text=sentence,
                ))
            elif kind == "ambiguous":
                # Saved but never listed — no owner.
                items.append(ActionItem(
                    owner="",
                    owner_name="",
                    owner_resolved=False,
                    action=action,
                    deadline=_parse_deadline(sentence),
                    confidence=0.3,
                    source_text=sentence,
                ))
            _ = start

    return _dedupe(items)


def _surrounding_sentence(text: str, pos: int) -> str:
    """Return the sentence containing ``pos``."""
    start = max(text.rfind(".", 0, pos), text.rfind("!", 0, pos),
                text.rfind("?", 0, pos)) + 1
    end = len(text)
    for sep in (".", "!", "?"):
        idx = text.find(sep, pos)
        if idx != -1:
            end = min(end, idx)
    return text[start:end].strip()


def _dedupe(items: list[ActionItem]) -> list[ActionItem]:
    """Drop near-duplicate items (same owner+action+source)."""
    seen = set()
    out: list[ActionItem] = []
    for it in items:
        key = (it.owner, _clean_action(it.action).lower(), it.source_text)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ── Store helpers (Phase 1) ───────────────────────────────────────

ACTION_META_FIELDS = (
    "action_item", "action_owner", "action_owner_name", "action_owner_resolved",
    "action_deadline", "action_status", "action", "action_confidence",
)


def attach_to_entry(entry: dict, item: ActionItem) -> dict:
    """Write an action item's metadata onto an entry dict (in place)."""
    entry.setdefault("source_metadata", {})
    for k, v in item.to_metadata().items():
        entry["source_metadata"][k] = v
    # Add a person entity so action items surface in people-centric search.
    if item.owner:
        entities = entry.setdefault("entities", [])
        if not any(e.get("value") == item.owner for e in entities):
            entities.append({"type": "person", "value": item.owner})
    return entry


def is_action_entry(entry: dict) -> bool:
    """True if an entry carries action-item metadata."""
    md = entry.get("source_metadata") or {}
    return bool(md.get("action_item"))


def action_item_of(entry: dict) -> ActionItem | None:
    """Rehydrate the ActionItem stored on an entry, if any."""
    md = entry.get("source_metadata") or {}
    return ActionItem.from_metadata(md)


def list_open_tasks(store, owner: str = "") -> list[dict]:
    """Return open action-item entries, optionally filtered by owner.

    Phase 1 filters over the existing entry store via source_metadata;
    a dedicated index is deferred until scale demands it.
    """
    result = []
    for entry in store.load_all():
        md = entry.get("source_metadata") or {}
        if not md.get("action_item"):
            continue
        status = md.get("action_status", "open")
        if status != ActionStatus.OPEN.value:
            continue
        if owner and md.get("action_owner") != owner:
            continue
        result.append(entry)
    return result


def set_task_status(store, entry_id: str, status: ActionStatus) -> bool:
    """Set an entry's action status. Returns True if updated."""
    entry = store.get(entry_id)
    if not entry:
        return False
    md = entry.setdefault("source_metadata", {})
    if not md.get("action_item"):
        return False
    md["action_status"] = status.value
    store.save(entry)
    return True
