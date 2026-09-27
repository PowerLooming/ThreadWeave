# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Topic bundling (P4) — make the memory browsable, not just searchable.

Groups captured entries into topics by the entities (technology, system,
organization, person) they share, plus title keywords. A topic is a
browsable cluster of related entries, so a user can explore "everything we
said about PostgreSQL" without knowing the exact query.

The grouping is computed on demand from the entry store (no persistent
index needed at this scale). It is confidentiality-safe: topics are built
from the entries the requester is allowed to see, so a topic never leaks
entries a requester shouldn't see.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

# Entity types we treat as topic-forming. People are excluded by default
# (a "topic" is a subject, not a person) but can be included via config.
TOPIC_ENTITY_TYPES = ("technology", "system", "organization")

# Stop words dropped from title keyword extraction.
_TITLE_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "for", "on", "in", "with",
    "we", "our", "use", "using", "how", "why", "what", "decision", "about",
    "new", "from", "by", "at", "into", "over", "after", "when", "is", "are",
}


def _title_keywords(title: str, limit: int = 4) -> list[str]:
    """Extract the most meaningful keywords from a title.

    Lowercases, drops stop words, keeps words of length >= 3. Returns up to
    ``limit`` keywords.
    """
    if not title:
        return []
    words = re.findall(r"[a-z0-9]+", title.lower())
    meaningful = [w for w in words if w not in _TITLE_STOP and len(w) >= 3]
    seen, out = set(), []
    for w in meaningful:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


def _entry_entities(entry: dict, include_people: bool = False) -> list[str]:
    """Return the topic-forming entity values for an entry."""
    out = []
    for ent in entry.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        etype = ent.get("type", "")
        val = (ent.get("value") or "").strip().lower()
        if not val:
            continue
        if etype in TOPIC_ENTITY_TYPES:
            out.append(val)
        elif include_people and etype == "person":
            out.append(val)
    return out


def _person_entity_values(entry: dict) -> set[str]:
    """Lowercased person entity values for an entry (empty when none).

    Used to keep person topics out of the near-duplicate merge: a person
    bucket describes the entries that person is on, never a subject.
    """
    out: set[str] = set()
    for ent in entry.get("entities") or []:
        if not isinstance(ent, dict) or ent.get("type") != "person":
            continue
        val = (ent.get("value") or "").strip().lower()
        if val:
            out.add(val)
    return out


def bundle_topics(
    entries: list[dict],
    *,
    include_people: bool = False,
    min_size: int = 2,
    max_topics: int = 50,
) -> list[dict]:
    """Group entries into topics by shared entities and title keywords.

    Args:
        entries: The entries to group (already confidentiality-filtered).
        include_people: Whether to treat person entities as topic-forming.
        min_size: A topic must have at least this many entries to be shown.
        max_topics: Cap on topics returned (most-populated first).

    Returns:
        A list of topic dicts, each:
            {
              "name": str,
              "entry_count": int,
              "entry_ids": [str, ...],
            }
        Sorted by entry_count descending, then name.
    """
    # Map each entity/keyword value -> set of entry ids. Person values are
    # tracked separately: people are an axis of their own, not a subject.
    bucket: dict[str, set[str]] = defaultdict(set)
    person_values: set[str] = set()
    id_to_entry = {e["id"]: e for e in entries}

    for entry in entries:
        eid = entry["id"]
        # Entities are the strongest signal.
        for val in _entry_entities(entry, include_people=include_people):
            bucket[val].add(eid)
        if include_people:
            person_values.update(_person_entity_values(entry))
        # Title keywords are a weaker signal but catch entity-less entries.
        for kw in _title_keywords(entry.get("title", "")):
            bucket[kw].add(eid)

    topics = []
    for name, ids in bucket.items():
        if len(ids) < min_size:
            continue
        topics.append({
            "name": name,
            "entry_count": len(ids),
            "entry_ids": sorted(ids),
        })

    # Merge near-duplicate buckets: when two topics contain (mostly) the
    # SAME entries, they are the same subject — e.g. an entity value
    # "postgresql" and a title keyword "postgres" both describing the same
    # 3 entries. Keep the one with the longer/entity-style name (the entity
    # is the stronger, more precise signal) and merge the rest's ids.
    #
    # Person topics are exempt in both directions. A person bucket always
    # covers exactly the entries its person appears in, so any subject
    # keyword over the same entries has a longer name and would absorb it —
    # which made include_people a silent no-op on real corpora. Person
    # topics neither absorb nor get absorbed.
    merged: dict[str, set[str]] = {}
    # Process most-populated first so the canonical name wins.
    topics.sort(key=lambda t: (-t["entry_count"], -len(t["name"]), t["name"]))
    for t in topics:
        t_ids = set(t["entry_ids"])
        if t["name"] in person_values:
            merged[t["name"]] = t_ids
            continue
        placed = False
        for canon, canon_ids in merged.items():
            if canon in person_values:
                continue
            # Overlap of >= 80% → same topic.
            if len(t_ids & canon_ids) >= 0.8 * max(len(t_ids), len(canon_ids), 1):
                merged[canon] = canon_ids | t_ids
                placed = True
                break
        if not placed:
            merged[t["name"]] = t_ids

    out = [
        {"name": name, "entry_count": len(ids), "entry_ids": sorted(ids)}
        for name, ids in merged.items()
    ]
    out.sort(key=lambda t: (-t["entry_count"], t["name"]))
    return out[:max_topics]


def topic_overlap(entry_a: dict, entry_b: dict) -> list[str]:
    """Return the shared topic terms between two entries (for pairing)."""
    a = set(_entry_entities(entry_a)) | set(_title_keywords(entry_a.get("title", "")))
    b = set(_entry_entities(entry_b)) | set(_title_keywords(entry_b.get("title", "")))
    return sorted(a & b)
