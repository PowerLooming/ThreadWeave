# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for the no-tenant demo palace (`threadweave demo`) and the
read-path guards it surfaced.

The demo exists so a first run needs no Microsoft 365 or Google
Workspace tenant. That means the dataset itself is public content: it
must stay fictional, and it must stay inside the sensitivity values the
read path understands.
"""

import os

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.confidentiality import SensitivityLevel, parse_sensitivity
from threadweave.demo_data import DEMO_TENANT, demo_entries, demo_summary
from threadweave.store import EntryStore

client = TestClient(app)


# ── the dataset ───────────────────────────────────────────────────


class TestDemoDataset:
    def test_entries_are_shaped_like_stored_entries(self):
        entries = demo_entries()
        assert len(entries) >= 25
        for entry in entries:
            assert entry["id"].startswith("demo-")
            assert entry["tenant_id"] == DEMO_TENANT
            assert entry["title"].strip()
            assert entry["content"].strip()
            assert entry["wing"] and entry["room"]
            assert entry["created_at"]
            assert entry["content_type"] in ("decision", "answer", "reference")

    def test_ids_are_unique(self):
        ids = [e["id"] for e in demo_entries()]
        assert len(ids) == len(set(ids))

    def test_sensitivities_are_enum_values(self):
        """A display-form value here crashes search (the live bug this
        closes): "hr-privileged" is not a SensitivityLevel, and the
        enum lookup sits inside the read path."""
        valid = {level.value for level in SensitivityLevel}
        for entry in demo_entries():
            assert entry["sensitivity"] in valid, entry["id"]

    def test_palace_spans_several_wings_and_rooms(self):
        summary = demo_summary(demo_entries())
        assert len(summary) >= 4
        assert len({room for rooms in summary.values() for room in rooms}) >= 8

    def test_dataset_is_fictional(self):
        """No real tenant, tenant domain or address belongs in public demo
        data.

        The check is deliberately structural rather than a list of banned
        strings: naming the terms here would publish the very identifiers
        the pre-push gate exists to keep out of the repository (it caught
        an earlier draft of this test for exactly that reason).
        """
        for entry in demo_entries():
            blob = (
                f"{entry['title']} {entry['content']} "
                f"{entry['author_id']} {entry['source_metadata']}"
            ).lower()
            assert "onmicrosoft.com" not in blob
            # Bare handles only: no addresses means no real domain, and no
            # plausible mailbox for a connector to write to.
            assert "@" not in blob
            assert entry["tenant_id"] == DEMO_TENANT
            assert " " not in entry["author_id"]


# ── seeding ───────────────────────────────────────────────────────


class TestDemoSeeding:
    def _seed(self, url: str) -> EntryStore:
        store = EntryStore(url=url)
        for entry in demo_entries():
            store.save(entry)
        return store

    def test_seed_writes_every_entry(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'demo.sqlite3'}"
        store = self._seed(url)
        assert store.count() == len(demo_entries())

    def test_reseeding_does_not_duplicate(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'demo.sqlite3'}"
        self._seed(url)
        store = self._seed(url)
        assert store.count() == len(demo_entries())

    def test_entries_round_trip(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'demo.sqlite3'}"
        self._seed(url)
        loaded = EntryStore(url=url).load_all()
        assert {e["id"] for e in loaded} == {e["id"] for e in demo_entries()}
        assert all(e["tenant_id"] == DEMO_TENANT for e in loaded)


# ── the CLI command ───────────────────────────────────────────────


class TestDemoCommand:
    def test_seed_only_writes_the_db(self, tmp_path, monkeypatch):
        from threadweave.cli import main

        db = tmp_path / "demo.sqlite3"
        env_before = {
            key: os.environ.get(key)
            for key in ("THREADWEAVE_ENTRY_DB", "MEMPALACE_PALACE_PATH",
                        "THREADWEAVE_PROFILE")
        }
        try:
            # cmd_demo sets these for the whole process on purpose (they
            # must be in place before the store or the API is imported),
            # so restore them here rather than leaking into other tests.
            monkeypatch.setattr("sys.argv", [
                "threadweave", "demo", "--db", str(db), "--reset", "--quiet",
            ])
            main()
            assert db.exists()
            assert EntryStore(url=f"sqlite:///{db}").count() == \
                len(demo_entries())
        finally:
            for key, value in env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_parser_accepts_the_documented_flags(self):
        from threadweave.cli import build_parser

        args = build_parser().parse_args([
            "demo", "--serve", "--port", "8123", "--profile", "org",
            "--reset", "--db", "C:/tmp/demo.sqlite3",
        ])
        assert args.command == "demo"
        assert args.serve is True
        assert args.port == 8123
        assert args.profile == "org"
        assert args.reset is True
        assert args.db == "C:/tmp/demo.sqlite3"


# ── the read-path guards ──────────────────────────────────────────


class TestUnknownSensitivity:
    def test_unknown_value_fails_closed(self):
        assert parse_sensitivity("hr-privileged") == SensitivityLevel.RESTRICTED
        assert parse_sensitivity("HR-PRIVILEGED") == SensitivityLevel.RESTRICTED
        assert parse_sensitivity("nonsense") == SensitivityLevel.RESTRICTED

    def test_known_values_are_untouched(self):
        assert parse_sensitivity("confidential") == SensitivityLevel.CONFIDENTIAL
        assert parse_sensitivity("hr_privileged") == SensitivityLevel.HR_PRIVILEGED
        assert parse_sensitivity(SensitivityLevel.PUBLIC) == \
            SensitivityLevel.PUBLIC

    def test_empty_value_keeps_the_default(self):
        assert parse_sensitivity(None) == SensitivityLevel.INTERNAL
        assert parse_sensitivity("") == SensitivityLevel.INTERNAL

    def test_search_survives_an_unknown_sensitivity(self):
        """The live failure: one entry carrying a value the enum does not
        know made POST /api/v1/search answer 500 for every query, so a
        single bad row took search down for the whole tenant."""
        from threadweave import api as api_mod

        created = client.post("/api/v1/entries", json={
            "content": "Zarquon release notes live in the shared drive.",
            "wing": "engineering",
            "room": "release-notes-zarquon",
            "author_id": "demo",
            "source_type": "manual",
        })
        assert created.status_code == 201
        entry_id = created.json()["id"]
        api_mod._memory_store[entry_id]["sensitivity"] = "hr-privileged"

        response = client.post("/api/v1/search", json={"query": "Zarquon"})
        assert response.status_code == 200
        # Fail closed: the entry is withheld, and the request still works.
        assert response.json()["total"] == 0


class TestKeywordFallbackReach:
    def _entry(self, **overrides):
        payload = {
            "content": "Placeholder.",
            "wing": "engineering",
            "room": "general",
            "author_id": "demo",
            "source_type": "manual",
        }
        payload.update(overrides)
        response = client.post("/api/v1/entries", json=payload)
        assert response.status_code == 201
        return response.json()["id"]

    def test_query_matches_a_room_name(self):
        """Rooms are topics, so searching a topic must find its room even
        when no entry contains the literal word."""
        self._entry(content="Severity follows the customer's work.",
                    wing="support", room="escalation-triage-x")

        data = client.post("/api/v1/search", json={
            "query": "escalation-triage-x"
        }).json()
        assert data["total"] >= 1
        assert any(r["room"] == "escalation-triage-x"
                   for r in data["results"])

    def test_query_matches_a_different_word_form(self):
        """`deployment` must find `deploy`: substring matching alone misses
        a query word that is longer than the one in the text."""
        self._entry(content="Never deplox on a Friday without a rollback.",
                    title="Friday deplox rule",
                    room="deplox-policy-x")

        data = client.post("/api/v1/search", json={
            "query": "deploxment"
        }).json()
        assert data["total"] >= 1

    def test_exact_hits_still_outrank_topic_hits(self):
        """Topic and word-form matches are tie-breakers, not winners: an
        exact content hit must come first."""
        exact = self._entry(content="The quuxnil setting governs retries.",
                            room="general")
        self._entry(content="Unrelated note about scheduling.",
                    room="quuxnil-policy-x")

        results = client.post("/api/v1/search", json={
            "query": "quuxnil"
        }).json()["results"]
        assert results
        assert results[0]["id"] == exact

    def test_short_words_do_not_pull_in_the_palace(self):
        """Words under four characters never take the prefix path."""
        data = client.post("/api/v1/search", json={"query": "xyz"}).json()
        assert data["total"] == 0
