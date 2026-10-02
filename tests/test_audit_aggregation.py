# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Repeated audit events fold into one row instead of burying the signal.

Measured need: a retrying client wrote 12,248 identical denials next to 58 real
refusals, and every read showed the flood and hid the refusals. Folding happens
at write time, so the operator's view is right by construction rather than from
a query someone has to remember to write.
"""

from __future__ import annotations

import sqlite3

import pytest

from threadweave.confidentiality import AuditLog, RequesterContext

ALICE = RequesterContext(person_id="alice", wing="engineering")
BOB = RequesterContext(person_id="bob", wing="billing")
ENTRY = {"id": "e1", "sensitivity": "confidential", "wing": "engineering"}


@pytest.fixture
def audit(tmp_path):
    return AuditLog(db_path=str(tmp_path / "audit.sqlite3"))


def deny(audit, reason="Insufficient clearance", requester=ALICE, entry=ENTRY):
    audit.log_denied(requester, entry, reason)


def test_identical_denials_fold_into_one_row(audit):
    for _ in range(500):
        deny(audit)
    assert audit.count == 1
    assert audit.event_count == 500
    row = audit.get_recent(1)[0]
    assert row["count"] == 500
    assert row["first_seen"] <= row["timestamp"], "a folded run keeps its start"


def test_a_flood_does_not_bury_the_real_refusals(audit):
    for _ in range(300):
        deny(audit, "Insufficient clearance")
    deny(audit, "Wing mismatch")
    deny(audit, "Revoked entry", requester=BOB)
    audit.log_access(
        ALICE, {"id": "e2", "sensitivity": "hr_privileged", "wing": "hr"}, "view"
    )

    recent = audit.get_recent(50)
    assert len(recent) == 4, "the retry loop is one row, not three hundred"
    reasons = {r["reason"] for r in recent if r["action"] == "denied"}
    assert {"Insufficient clearance", "Wing mismatch", "Revoked entry"} <= reasons
    assert audit.event_count == 303


def test_a_different_reason_starts_a_new_row(audit):
    deny(audit, "Insufficient clearance")
    deny(audit, "Wing mismatch")
    deny(audit, "Insufficient clearance")
    assert audit.count == 3


def test_window_zero_keeps_every_event(audit, monkeypatch):
    monkeypatch.setenv("THREADWEAVE_AUDIT_AGGREGATE_WINDOW", "0")
    for _ in range(5):
        deny(audit)
    assert audit.count == 5
    assert audit.event_count == 5


def test_a_late_repeat_starts_a_new_row(audit, monkeypatch):
    """A retry loop that comes back an hour later is not the same run."""
    monkeypatch.setenv("THREADWEAVE_AUDIT_AGGREGATE_WINDOW", "60")
    deny(audit)
    audit._db.execute(
        "UPDATE audit_entries SET timestamp = '2020-01-01T00:00:00+00:00'"
    )
    audit._db.commit()
    deny(audit)
    assert audit.count == 2, "the window is measured from the last event"


def test_unparsable_window_falls_back_to_the_default(audit, monkeypatch):
    monkeypatch.setenv("THREADWEAVE_AUDIT_AGGREGATE_WINDOW", "not-a-number")
    for _ in range(3):
        deny(audit)
    assert audit.count == 1, "a typo must not silently disable the aggregation"


def test_in_memory_fallback_folds_too(monkeypatch):
    def boom(self, path):
        raise RuntimeError("no db")

    monkeypatch.setattr(AuditLog, "_init_db", boom)
    audit = AuditLog()
    for _ in range(4):
        deny(audit)
    assert audit.count == 1
    assert audit.event_count == 4
    assert audit.get_recent(10)[0]["count"] == 4


def test_an_older_database_is_migrated_and_keeps_its_rows(tmp_path):
    """A trail written before the columns existed must stay readable."""
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE audit_entries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "requester_id TEXT NOT NULL, requester_wing TEXT NOT NULL, "
        "action TEXT NOT NULL, entry_id TEXT NOT NULL, "
        "entry_sensitivity TEXT NOT NULL, entry_wing TEXT NOT NULL, "
        "reason TEXT NOT NULL DEFAULT '', "
        "tenant_id TEXT NOT NULL DEFAULT 'default', "
        "ip_hash TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO audit_entries (timestamp, requester_id, requester_wing, action, "
        "entry_id, entry_sensitivity, entry_wing, reason) VALUES "
        "('2026-08-08T10:00:00+00:00', 'alice', 'eng', 'denied', 'e1', "
        "'confidential', 'eng', 'old row')"
    )
    conn.commit()
    conn.close()

    audit = AuditLog(db_path=path)
    rows = audit.get_recent(5)
    assert len(rows) == 1
    assert rows[0]["reason"] == "old row"
    assert rows[0]["count"] == 1
    assert rows[0]["first_seen"] == rows[0]["timestamp"]
    assert audit.event_count == 1

    deny(audit)
    assert audit.count == 2, "new writes land beside the migrated row"
    assert audit.event_count == 2
