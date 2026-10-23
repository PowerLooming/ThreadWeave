# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""The capture-quality sheet: deterministic, in range, per wing, markable.

The script is the measurement half of the second pilot condition, so what is
asserted here is what the pilot team relies on: the same week always produces
the same sheet, nothing outside the range appears, the sample is small and
capped per wing, the detector's counts are reported for comparison, and a
missing store is an error rather than an empty week.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from threadweave.store import EntryStore

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "capture_quality_sample.py"

SINCE = "2026-10-16"
UNTIL = "2026-10-22"


def _entry(entry_id: str, wing: str, kind: str, created_at: str, content: str,
           room: str = "ledger") -> dict:
    return {
        "id": entry_id, "wing": wing, "room": room, "scope": "team",
        "source_type": "email", "author_id": "alice", "title": "",
        "created_at": created_at, "entities": [], "content_type": kind,
        "has_pii": False, "tenant_id": "pilotco", "sensitivity": "internal",
        "content": content,
    }


ENTRIES = [
    # pilot wing: five in range, plus one on each side of the boundary.
    _entry("p1", "pilot", "decision", "2026-10-16T08:00:00+00:00",
           "We decided the reconciliation ledger moves onto postgres."),
    _entry("p2", "pilot", "decision", "2026-10-17T09:00:00+00:00",
           "We decided the depot holds parcels over thirty kilos."),
    _entry("p3", "pilot", "answer", "2026-10-18T10:00:00+00:00",
           "The freight invoice is paid on the first working day of the month."),
    _entry("p4", "pilot", "chat", "2026-10-21T11:00:00+00:00",
           "Morning, hope the standup was not too long."),
    _entry("p5", "pilot", "question", "2026-10-22T23:59:00+00:00",
           "Does anyone know where the escrow checklist lives now?"),
    _entry("p6", "pilot", "decision", "2026-10-23T00:00:00+00:00",
           "ZEBRA boundary entry that is one second outside the range."),
    _entry("p7", "pilot", "decision", "2026-10-15T23:59:00+00:00",
           "YAK boundary entry that is one second before the range."),
    # finance wing: two in range.
    _entry("f1", "finance", "reference", "2026-10-19T12:00:00+00:00",
           "Link to the vendor onboarding checklist template."),
    _entry("f2", "finance", "answer", "2026-10-20T12:00:00+00:00",
           "Invoices are reconciled against the purchase order weekly.", room="inbox"),
    # A capture whose timestamp cannot be read is reported, not silently lost.
    _entry("u1", "pilot", "answer", "not-a-timestamp", "An entry with a broken capture time."),
]


@pytest.fixture
def store(tmp_path) -> str:
    path = tmp_path / "entries.sqlite3"
    entry_store = EntryStore(f"sqlite:///{path.as_posix()}")
    for entry in ENTRIES:
        entry_store.save(entry)
    return str(path)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


def _sheet(store_path: str, *args: str) -> str:
    result = _run("--store", store_path, "--since", SINCE, "--until", UNTIL, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _table(sheet: str) -> dict[str, list[int]]:
    """The per-wing detector table as {wing: [captured, decision, ...]}."""
    lines = sheet.splitlines()
    start = lines.index("WHAT THE DETECTOR THOUGHT (whole range, before your marking)")
    rows: dict[str, list[int]] = {}
    for line in lines[start + 1:]:
        if not line.strip():
            break
        parts = line.split()
        if parts[0] == "wing":
            continue
        rows[parts[0]] = [int(value) for value in parts[1:]]
    return rows


def test_the_same_store_and_range_always_print_the_same_sheet(store):
    assert _sheet(store) == _sheet(store)


def test_nothing_outside_the_date_range_appears(store):
    sheet = _sheet(store)
    assert "ZEBRA" not in sheet
    assert "YAK" not in sheet
    assert "reconciliation ledger" in sheet
    assert "inclusive (UTC dates)" in sheet


def test_the_sample_is_capped_per_wing(store):
    sheet = _sheet(store, "--per-wing", "2")
    # pilot has 5 in range -> 2 sampled; finance has 2 -> 2; the whole sheet
    # therefore carries four entry boxes, not one per captured entry.
    boxes = [line for line in sheet.splitlines() if line.startswith("[ ] ")]
    assert len(boxes) == 4
    assert "pilot - 5 captured, 2 sampled" in sheet
    assert "finance - 2 captured, 2 sampled" in sheet


def test_detector_counts_are_reported_per_wing_and_per_type(store):
    rows = _table(_sheet(store))
    # captured, decision, answer, question, reference, chat, other
    assert rows["pilot"] == [5, 2, 1, 1, 0, 1, 0]
    assert rows["finance"] == [2, 0, 1, 0, 1, 0, 0]
    assert rows["total"] == [7, 2, 2, 1, 1, 1, 0]


def test_decisions_versus_noise_is_stated_for_the_marker(store):
    sheet = _sheet(store)
    assert "decisions + answers: 4" in sheet
    assert "chat + questions + references: 3" in sheet
    assert "detector's guesses" in sheet


def test_an_unreadable_capture_time_is_counted_not_dropped_silently(store):
    assert "skipped  : 1 entries with no readable capture time" in _sheet(store)


def test_the_sheet_carries_the_rubric_and_a_tally_to_fill_in(store):
    sheet = _sheet(store)
    assert "K = knowledge worth keeping" in sheet
    assert "N = noise" in sheet
    assert "? = cannot tell from the excerpt" in sheet
    assert "precision = K / (K + N)" in sheet
    assert "docs/capture-quality.md" in sheet
    assert "TALLY - fill this in and send it back with the sheet" in sheet
    # Every sampled entry is on its own line, with the detector's label for
    # comparison and the id needed to open it.
    assert "[ ]  1  " in sheet
    assert "id=p1" in sheet or "id=f1" in sheet


def test_a_wing_filter_narrows_the_sheet(store):
    sheet = _sheet(store, "--wing", "finance")
    assert "finance - 2 captured" in sheet
    assert "wing: pilot" not in sheet
    assert "id=f1" in sheet


def test_an_empty_range_is_reported_as_a_result(store):
    result = _run("--store", store, "--since", "2026-11-01", "--until", "2026-11-07")
    assert result.returncode == 0, result.stderr
    assert "No entries were captured in this range." in result.stdout


def test_a_missing_store_is_an_error_not_an_empty_week(tmp_path):
    result = _run("--store", str(tmp_path / "typo.sqlite3"),
                  "--since", SINCE, "--until", UNTIL)
    assert result.returncode == 2
    assert "no entry store at" in result.stderr
