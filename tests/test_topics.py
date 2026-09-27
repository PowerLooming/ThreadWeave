# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for topic bundling (P4)."""

import pytest

from threadweave.topics import bundle_topics, _title_keywords, topic_overlap


def _entry(eid, title="", entities=None):
    return {"id": eid, "title": title, "entities": entities or []}


class TestTitleKeywords:
    def test_returns_meaningful_words(self):
        out = _title_keywords("We decided to use PostgreSQL for analytics")
        assert "postgresql" in out and "analytics" in out

    def test_drops_stopwords(self):
        out = _title_keywords("The new platform decision about our stack")
        assert "the" not in out and "about" not in out and "our" not in out
        assert "platform" in out

    def test_empty_title(self):
        assert _title_keywords("") == []

    def test_short_words_dropped(self):
        out = _title_keywords("go to the db")
        assert all(len(w) >= 3 for w in out)


class TestBundleTopics:
    def test_groups_by_shared_entity(self):
        entries = [
            _entry("e1", "Postgres decision", [{"type": "technology", "value": "postgresql"}]),
            _entry("e2", "Postgres migration", [{"type": "technology", "value": "postgresql"}]),
            _entry("e3", "unrelated", []),
        ]
        topics = bundle_topics(entries)
        postgres = [t for t in topics if t["name"] == "postgresql"]
        assert postgres and postgres[0]["entry_count"] == 2
        assert set(postgres[0]["entry_ids"]) == {"e1", "e2"}

    def test_min_size_filters_small_groups(self):
        entries = [
            _entry("e1", "Kafka stream", [{"type": "technology", "value": "kafka"}]),
            _entry("e2", "other", []),
        ]
        # min_size=2 → kafka has only 1 entry, excluded
        assert bundle_topics(entries, min_size=2) == []

    def test_people_excluded_by_default(self):
        entries = [
            _entry("e1", "talk", [{"type": "person", "value": "adele"}]),
            _entry("e2", "talk2", [{"type": "person", "value": "adele"}]),
        ]
        assert bundle_topics(entries) == []
        # with include_people → topic appears
        topics = bundle_topics(entries, include_people=True)
        assert any(t["name"] == "adele" for t in topics)

    def test_person_topic_survives_the_near_duplicate_merge(self):
        """Person buckets must not be absorbed by a keyword bucket covering the
        same entries. The keyword name is longer, so it won the merge and the
        person topic vanished — include_people was a silent no-op on real
        corpora, where anyone named in an entry is also named in its title."""
        entries = [
            _entry("e1", "Deadline review for the firewall change",
                   [{"type": "person", "value": "adele"}]),
            _entry("e2", "Deadline review for the audit export",
                   [{"type": "person", "value": "adele"}]),
        ]
        assert not any(t["name"] == "adele" for t in bundle_topics(entries))

        topics = bundle_topics(entries, include_people=True)
        adele = next((t for t in topics if t["name"] == "adele"), None)
        assert adele is not None
        assert set(adele["entry_ids"]) == {"e1", "e2"}

    def test_person_topic_does_not_absorb_subject_topics(self):
        """Both axes survive together: the person and the subject."""
        entries = [
            _entry("e1", "Postgres upgrade window",
                   [{"type": "person", "value": "adele"},
                    {"type": "technology", "value": "postgresql"}]),
            _entry("e2", "Postgres upgrade rollback",
                   [{"type": "person", "value": "adele"},
                    {"type": "technology", "value": "postgresql"}]),
        ]
        names = {t["name"] for t in bundle_topics(entries, include_people=True)}
        assert {"adele", "postgresql"} <= names

    def test_groups_by_title_keyword(self):
        entries = [
            _entry("e1", "Database migration plan"),
            _entry("e2", "Database backup strategy"),
        ]
        topics = bundle_topics(entries)
        db = [t for t in topics if t["name"] == "database"]
        assert db and db[0]["entry_count"] == 2

    def test_sorted_by_popularity(self):
        entries = [
            _entry("e1", "Postgres", [{"type": "technology", "value": "postgresql"}]),
            _entry("e2", "Postgres", [{"type": "technology", "value": "postgresql"}]),
            _entry("e3", "Kafka", [{"type": "technology", "value": "kafka"}]),
            _entry("e4", "Postgres", [{"type": "technology", "value": "postgresql"}]),
            _entry("e5", "Kafka", [{"type": "technology", "value": "kafka"}]),
        ]
        topics = bundle_topics(entries)
        assert topics[0]["name"] == "postgresql"  # 3 entries
        assert topics[1]["name"] == "kafka"       # 2 entries

    def test_max_topics_cap(self):
        entries = []
        for i in range(10):
            entries.append(_entry(f"e{i}", f"topic{i} alpha"))
        # many single-entity topics; with min_size=2 mostly filtered
        # give 5 topics of size 2
        entries2 = []
        for t in range(5):
            entries2 += [
                _entry(f"a{t}", f"Topic{t} X", [{"type": "system", "value": f"sys{t}"}]),
                _entry(f"b{t}", f"Topic{t} Y", [{"type": "system", "value": f"sys{t}"}]),
            ]
        topics = bundle_topics(entries2, max_topics=3)
        assert len(topics) == 3


class TestTopicOverlap:
    def test_finds_shared_terms(self):
        a = _entry("e1", "Database migration", [{"type": "technology", "value": "postgresql"}])
        b = _entry("e2", "Database backup", [{"type": "technology", "value": "postgresql"}])
        shared = topic_overlap(a, b)
        assert "database" in shared and "postgresql" in shared

    def test_no_overlap(self):
        a = _entry("e1", "Kafka streaming")
        b = _entry("e2", "Postgres storage")
        assert topic_overlap(a, b) == []
