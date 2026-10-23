#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Capture-quality sample: a sheet a non-engineer can mark up in ten minutes.

This is the measurement half of the second pilot condition ("Capture-quality
measurement: decisions found vs. missed, sampled weekly from the pilot team").
It answers one question about a week of capture: of what ThreadWeave stored,
how much was knowledge a person would miss if it were gone, and how much was
noise.

It does not answer that itself. It prints a small, fixed sample per wing with a
blank box in front of every entry, and the pilot team writes K, N or ? in each
box. That is deliberate: the detector's own labels are what is being checked,
so the script prints them for comparison and never scores itself. No model
calls, no network, no writes to the store. The protocol, the rubric and the
number that gets reported are in ``docs/capture-quality.md``.

Deterministic: the same store and the same date range always print the same
sample. Entries are ordered by capture time (then id) inside each wing and
picked at even intervals across that order, so the sample spreads over the week
instead of clustering at the start. Two runs of the same week are comparable
line for line, which is what lets a marked-up sheet be filed as evidence.

Usage::

    python scripts/capture_quality_sample.py --since 2026-10-16 --until 2026-10-22
    python scripts/capture_quality_sample.py --wing pilot --per-wing 6 --out sheet.txt
    python scripts/capture_quality_sample.py --store /srv/weave/entries.sqlite3

Exit codes: 0 the sheet was printed (an empty range is a finding, not an
error), 2 the store could not be read.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

PREVIEW_CHARS = 110
DEFAULT_PER_WING = 5


class SampleError(Exception):
    """Something the operator can fix (bad path, bad date, unreadable store)."""


# The detector's five types, in the order the summary table shows them.
# "decision" and "answer" are what the product exists to keep; "chat",
# "question" and "reference" are context at best. The table reports them
# separately and never merges them into a score: the human marking does that.
TYPES = ("decision", "answer", "question", "reference", "chat", "other")
SIGNAL_TYPES = ("decision", "answer")
NOISE_TYPES = ("chat", "question", "reference")


def _repo_src_on_path() -> None:
    """Make ``threadweave`` importable when run from a checkout."""
    root = Path(__file__).resolve().parent.parent
    src = str(root / "src")
    if src not in sys.path and Path(src).is_dir():
        sys.path.insert(0, src)


def store_url(store: Optional[str]) -> str:
    """Resolve --store into a SQLAlchemy URL, failing loudly on a bad path.

    A bare path is only accepted when the file exists: a typo would otherwise
    create an empty SQLite file and the sheet would report a week of silence
    that never happened.
    """
    if not store:
        return os.environ.get("THREADWEAVE_ENTRY_DB", "").strip() or str(
            Path.home() / ".threadweave" / "entries.sqlite3"
        )
    if "://" in store:
        return store
    path = Path(os.path.expanduser(store))
    if not path.is_file():
        raise SampleError(
            f"no entry store at {path}\n"
            "Pass --store with the SQLite file the server writes "
            "(usually ~/.threadweave/entries.sqlite3), or a URL such as "
            "postgresql://user@host/threadweave."
        )
    return f"sqlite:///{path.resolve().as_posix()}"


def as_url(store: str) -> str:
    """A bare path from the environment is still a SQLite file."""
    if "://" in store:
        return store
    path = Path(os.path.expanduser(store))
    if not path.is_file():
        raise SampleError(f"no entry store at {path}")
    return f"sqlite:///{path.resolve().as_posix()}"


def parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise SampleError(f"not a date: {raw!r} (expected YYYY-MM-DD)")


def entry_time(raw: str) -> Optional[datetime]:
    """Parse a stored created_at into an aware UTC datetime, or None."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def bucket(content_type: str) -> str:
    """The summary-table column for one entry's stored type."""
    kind = (content_type or "").strip().lower()
    return kind if kind in TYPES[:-1] else "other"


def pick_evenly(items: list[dict], count: int) -> list[dict]:
    """Take `count` entries spread evenly across an ordered list.

    Deterministic and order-preserving, so the same week always yields the
    same sample and a re-run can be diffed against a marked-up sheet.
    """
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit].rsplit(" ", 1)[0]
    return (cut or collapsed[:limit]) + "..."


def collect(entries: Iterable[dict], since: date, until: date) -> tuple[list[dict], int]:
    """Entries captured inside [since, until] (inclusive dates, UTC).

    Returns the entries and the number skipped because their created_at could
    not be read, so a broken timestamp is visible instead of silently absent.
    """
    window_start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    window_end = datetime.combine(until + timedelta(days=1), datetime.min.time(),
                                  tzinfo=timezone.utc)
    kept: list[dict] = []
    undated = 0
    for entry in entries:
        moment = entry_time(entry.get("created_at", ""))
        if moment is None:
            undated += 1
            continue
        if window_start <= moment < window_end:
            item = dict(entry)
            item["_moment"] = moment
            kept.append(item)
    return kept, undated


def group_by_wing(entries: list[dict]) -> dict[str, list[dict]]:
    wings: dict[str, list[dict]] = {}
    for entry in entries:
        wings.setdefault(entry.get("wing") or "(no wing)", []).append(entry)
    for items in wings.values():
        items.sort(key=lambda e: (e["_moment"], e.get("id", "")))
    return wings


def render(entries: list[dict], undated: int, *, since: date, until: date,
           per_wing: int, store: str, wing_filter: list[str]) -> str:
    """Build the printable sheet. Pure function: same input, same text."""
    wings = group_by_wing(entries)
    if wing_filter:
        wanted = {w.strip().lower() for w in wing_filter if w.strip()}
        wings = {w: items for w, items in wings.items() if w.lower() in wanted}
    names = sorted(wings)
    total = sum(len(v) for v in wings.values())

    width = max([len(n) for n in names] + [4])
    sampled = {name: pick_evenly(wings[name], per_wing) for name in names}

    lines: list[str] = []
    lines.append("ThreadWeave capture-quality sample")
    lines.append("=" * 34)
    lines.append(f"store    : {store}")
    lines.append(f"range    : {since.isoformat()} to {until.isoformat()} inclusive (UTC dates)")
    lines.append(f"captured : {total} entries in {len(names)} wing(s), "
                 f"{sum(len(v) for v in sampled.values())} sampled "
                 f"(up to {per_wing} per wing)")
    if undated:
        lines.append(f"skipped  : {undated} entries with no readable capture time")
    lines.append("")
    lines.append("HOW TO MARK THIS UP (about ten minutes)")
    lines.append("  For each sampled entry, write one letter in the [ ] box:")
    lines.append("    K = knowledge worth keeping (a decision, how something works, why)")
    lines.append("    N = noise (chatter, a bare question, an automated notice, a link)")
    lines.append("    ? = cannot tell from the excerpt")
    lines.append("  Then fill in the tally at the bottom and send it back.")
    lines.append("  The reported number is precision = K / (K + N).")
    lines.append("  Rubric with examples: docs/capture-quality.md")
    lines.append("")

    if not total:
        lines.append("No entries were captured in this range.")
        lines.append("That is itself a result: check the connector is polling, or widen")
        lines.append("the range with --since/--until.")
        return "\n".join(lines)

    # What the detector thought, over the whole range. Printed before the
    # sample so the marker sees it only afterwards; it is the thing being
    # checked, not a hint.
    header = (f"  {'wing'.ljust(width)}  {'captured'.rjust(8)}  " +
              "  ".join(t.rjust(9) for t in TYPES))
    lines.append("WHAT THE DETECTOR THOUGHT (whole range, before your marking)")
    lines.append(header)
    totals = {t: 0 for t in TYPES}
    for name in names:
        counts = {t: 0 for t in TYPES}
        for entry in wings[name]:
            counts[bucket(entry.get("content_type", ""))] += 1
        for t in TYPES:
            totals[t] += counts[t]
        lines.append(f"  {name.ljust(width)}  {str(len(wings[name])).rjust(8)}  " +
                     "  ".join(str(counts[t]).rjust(9) for t in TYPES))
    lines.append(f"  {'total'.ljust(width)}  {str(total).rjust(8)}  " +
                 "  ".join(str(totals[t]).rjust(9) for t in TYPES))
    signal = sum(totals[t] for t in SIGNAL_TYPES)
    noise = sum(totals[t] for t in NOISE_TYPES)
    lines.append("")
    lines.append(f"  decisions + answers: {signal}    chat + questions + references: {noise}"
                 f"    untagged/other: {totals['other']}")
    lines.append("  These are the detector's guesses. The K/N marking below is the evidence.")
    lines.append("")

    for name in names:
        rows = sampled[name]
        lines.append(f"=== wing: {name} - {len(wings[name])} captured, "
                     f"{len(rows)} sampled ===")
        for index, entry in enumerate(rows, start=1):
            moment = entry["_moment"].strftime("%Y-%m-%d %H:%M")
            kind = bucket(entry.get("content_type", ""))
            room = entry.get("room") or "-"
            lines.append(f"[ ] {index:>2}  {moment}  {kind:<9}  room={room:<14}  "
                         f"id={entry.get('id', '')}")
            lines.append(f"        {one_line(entry.get('content', ''))}")
        lines.append("")

    lines.append("TALLY - fill this in and send it back with the sheet")
    lines.append(f"  {'wing'.ljust(width)}  sampled   K   N   ?   precision")
    for name in names:
        count = len(sampled[name])
        lines.append(f"  {name.ljust(width)}  {str(count).rjust(7)}   _   _   _   _______")
    lines.append(f"  {'all'.ljust(width)}  {str(sum(len(v) for v in sampled.values())).rjust(7)}"
                 "   _   _   _   _______")
    lines.append("")
    lines.append("Precision is K / (K + N) over the boxes you filled; a '?' is counted")
    lines.append("in neither and reported separately. One number per week, per wing.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a capture-quality sample sheet for one date range.",
        epilog="The protocol and the rubric are in docs/capture-quality.md.",
    )
    parser.add_argument("--store", default=None,
                        help="entry store: a SQLite path or a database URL "
                             "(default: THREADWEAVE_ENTRY_DB, then "
                             "~/.threadweave/entries.sqlite3)")
    parser.add_argument("--since", default=None,
                        help="first day to include, YYYY-MM-DD (default: six days "
                             "before --until)")
    parser.add_argument("--until", default=None,
                        help="last day to include, YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--per-wing", type=int, default=DEFAULT_PER_WING,
                        help=f"entries to sample per wing (default {DEFAULT_PER_WING})")
    parser.add_argument("--wing", action="append", default=[],
                        help="restrict to this wing; repeat for more than one")
    parser.add_argument("--out", default=None,
                        help="write the sheet to this file instead of stdout")
    args = parser.parse_args(argv)

    try:
        until = parse_day(args.until) if args.until else datetime.now(timezone.utc).date()
        since = parse_day(args.since) if args.since else until - timedelta(days=6)
        if since > until:
            parser.error("--since is after --until")
        if args.per_wing < 0:
            parser.error("--per-wing cannot be negative")

        url = as_url(store_url(args.store))
        _repo_src_on_path()
        try:
            from threadweave.store import EntryStore
        except Exception as exc:  # pragma: no cover - packaging problem, not data
            raise SampleError(f"cannot import threadweave ({exc})")
        try:
            entries = EntryStore(url).load_all()
        except Exception as exc:
            raise SampleError(f"cannot read the store at {url}: {exc}")
    except SampleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    kept, undated = collect(entries, since, until)
    sheet = render(kept, undated, since=since, until=until,
                   per_wing=args.per_wing, store=url, wing_filter=args.wing)

    if args.out:
        Path(args.out).expanduser().write_text(sheet + "\n", encoding="utf-8")
        print(f"wrote {args.out} ({len(kept)} entries in range)")
    else:
        print(sheet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
