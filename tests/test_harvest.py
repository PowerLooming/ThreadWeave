# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for offboarding harvester and onboarding brief generator.
"""

import json
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from threadweave.connectors.gws.harvest import (
    OffboardingHarvester,
    HarvestStats,
    generate_onboarding_brief,
    HARVEST_QUERIES,
)
from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials


# ═══════════════════════════════════════════════════════════════════════
# HarvestStats Tests
# ═══════════════════════════════════════════════════════════════════════

class TestHarvestStats:
    def test_empty_stats(self):
        s = HarvestStats()
        assert s.emails_scanned == 0
        assert s.total_saved == 0
        d = s.to_dict()
        assert d["emails_scanned"] == 0

    def test_merge_combines(self):
        s1 = HarvestStats(
            emails_scanned=100, emails_processed=50, emails_saved=30,
            chat_messages_scanned=200, chat_messages_saved=10,
        )
        s2 = HarvestStats(
            emails_scanned=50, emails_processed=20, emails_saved=15,
            drive_files_scanned=30, drive_files_saved=5,
        )
        combined = HarvestStats()
        combined.emails_scanned += s1.emails_scanned + s2.emails_scanned
        combined.emails_saved += s1.emails_saved + s2.emails_saved
        combined.chat_messages_scanned += s1.chat_messages_scanned + s2.chat_messages_scanned
        combined.chat_messages_saved += s1.chat_messages_saved + s2.chat_messages_saved
        combined.drive_files_scanned += s2.drive_files_scanned
        combined.drive_files_saved += s2.drive_files_saved
        combined.total_saved += combined.emails_saved + combined.chat_messages_saved + combined.drive_files_saved

        assert combined.emails_scanned == 150
        assert combined.emails_saved == 45
        assert combined.total_saved == 60

    def test_to_dict_includes_all_fields(self):
        s = HarvestStats(
            user_email="alice@co.com",
            emails_scanned=100, emails_saved=50,
            total_saved=50,
            errors=["test error"],
        )
        d = s.to_dict()
        assert d["user_email"] == "alice@co.com"
        assert d["emails_scanned"] == 100
        assert d["emails_saved"] == 50
        assert "errors" in d
        assert d["total_saved"] == 50


# ═══════════════════════════════════════════════════════════════════════
# Harvest Queries Tests
# ═══════════════════════════════════════════════════════════════════════

class TestHarvestQueries:
    def test_queries_exist(self):
        """Verify we have knowledge-bearing search queries."""
        assert "decisions" in HARVEST_QUERIES
        assert "answers" in HARVEST_QUERIES
        assert "processes" in HARVEST_QUERIES
        assert "client_context" in HARVEST_QUERIES
        assert "long_threads" in HARVEST_QUERIES
        assert "thread_participation" in HARVEST_QUERIES

    def test_decision_query_has_sent_filter(self):
        assert "in:sent" in HARVEST_QUERIES["decisions"]

    def test_queries_are_formattable(self):
        """Thread participation query uses {email} placeholder."""
        assert "{email}" in HARVEST_QUERIES["thread_participation"]
        formatted = HARVEST_QUERIES["thread_participation"].format(
            email="alice@co.com"
        )
        assert "alice@co.com" in formatted


# ═══════════════════════════════════════════════════════════════════════
# OffboardingHarvester Tests
# ═══════════════════════════════════════════════════════════════════════

class TestOffboardingHarvester:
    def _mock_auth(self, tmp_path):
        """Create a mock GWSAuth with a temp credentials file."""
        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')
        creds = GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@co.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        return GWSAuth(creds)

    def test_constructor(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(auth)
        assert harvester.threadweave_url == "http://localhost:8000"
        assert harvester.checkpoint_dir.exists()

    def test_auth_for_user_switches_account(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(auth)

        user_auth = harvester._auth_for_user("alice@co.com")
        assert user_auth._credentials.delegated_account == "alice@co.com"
        # Original auth unchanged
        assert auth._credentials.delegated_account == "admin@co.com"

    def test_checkpoint_save_and_load(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(
            auth, checkpoint_dir=str(tmp_path / "checkpoints"),
        )

        harvester._save_checkpoint("alice@co.com", {"msg1", "msg2", "msg3"})
        loaded = harvester._load_checkpoint("alice@co.com")
        assert loaded == {"msg1", "msg2", "msg3"}

    def test_checkpoint_empty_for_new_user(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(
            auth, checkpoint_dir=str(tmp_path / "checkpoints"),
        )
        loaded = harvester._load_checkpoint("nonexistent@co.com")
        assert loaded == set()

    def test_report_save_and_load(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(
            auth, checkpoint_dir=str(tmp_path / "checkpoints"),
        )

        stats = HarvestStats(
            user_email="bob@co.com",
            emails_scanned=500, emails_saved=120,
            total_saved=120,
        )
        harvester._save_report("bob@co.com", stats)

        report = harvester.generate_report("bob@co.com")
        assert report is not None
        assert report["user_email"] == "bob@co.com"
        assert report["emails_saved"] == 120

    def test_report_none_for_missing(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(auth)
        report = harvester.generate_report("noone@co.com")
        assert report is None

    def test_merge_stats(self, tmp_path):
        auth = self._mock_auth(tmp_path)
        harvester = OffboardingHarvester(auth)

        combined = HarvestStats()
        source = HarvestStats(
            emails_scanned=100, emails_processed=50, emails_saved=30,
        )
        harvester._merge_stats(combined, source)
        assert combined.emails_scanned == 100
        assert combined.emails_processed == 50
        assert combined.emails_saved == 30
        assert combined.total_saved == 30


# ═══════════════════════════════════════════════════════════════════════
# Onboarding Brief Tests
# ═══════════════════════════════════════════════════════════════════════

class TestOnboardingBrief:
    @patch("threadweave.connectors.gws.harvest.requests.post")
    @patch("threadweave.connectors.gws.harvest.requests.get")
    def test_generates_brief_structure(self, mock_get, mock_post):
        """Brief should have all expected sections."""
        # Mock search responses
        mock_search_resp = MagicMock()
        mock_search_resp.status_code = 200
        mock_search_resp.json.return_value = {
            "results": [
                {
                    "id": "e1", "title": "Database Decision",
                    "content_preview": "We chose Postgres...",
                    "created_at": "2025-01-15T10:00:00",
                },
            ],
        }
        mock_post.return_value = mock_search_resp

        # Mock entry GET
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "e1", "content": "...", "author_id": "alice@co.com",
        }
        mock_get.return_value = mock_get_resp

        brief = generate_onboarding_brief(
            new_hire_email="bob@co.com",
            predecessor_email="alice@co.com",
            team_wing="engineering",
        )

        assert "new_hire" in brief
        assert brief["new_hire"] == "bob@co.com"
        assert brief["predecessor"] == "alice@co.com"
        assert brief["team"] == "engineering"
        assert "predecessor_knowledge" in brief
        assert "team_knowledge" in brief
        assert "recent_decisions" in brief
        assert "onboarding_checklist" in brief
        assert len(brief["onboarding_checklist"]) >= 1

    @patch("threadweave.connectors.gws.harvest.requests.post")
    def test_handles_api_unavailable(self, mock_post):
        """Brief should handle API being down gracefully."""
        mock_post.side_effect = Exception("Connection refused")

        brief = generate_onboarding_brief(
            new_hire_email="bob@co.com",
            predecessor_email="alice@co.com",
            team_wing="engineering",
        )

        # Should still return a valid brief with empty knowledge
        assert brief["predecessor_knowledge"] == []
        assert brief["team_knowledge"] == []
        assert brief["recent_decisions"] == []
        assert len(brief["onboarding_checklist"]) >= 1


# ═══════════════════════════════════════════════════════════════════════
# Harvest All — Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestHarvestAll:
    def test_harvest_all_aggregates_sources(self, tmp_path):
        """harvest_all should harvest from Gmail, Chat, and Drive, and merge stats."""
        from threadweave.connectors.gws.harvest import OffboardingHarvester, HarvestStats
        from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials

        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')
        creds = GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@co.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        auth = GWSAuth(creds)

        harvester = OffboardingHarvester(
            auth,
            threadweave_url="http://localhost:8000",
            checkpoint_dir=str(tmp_path / "checkpoints"),
        )

        def mock_gmail(user_email, max_messages):
            return HarvestStats(
                user_email=user_email,
                emails_scanned=100, emails_processed=50, emails_saved=30,
                total_saved=30,
            )

        def mock_chat(user_email, **kwargs):
            return HarvestStats(
                user_email=user_email,
                chat_messages_scanned=200, chat_messages_saved=15,
                total_saved=15,
            )

        def mock_drive(user_email, max_files):
            return HarvestStats(
                user_email=user_email,
                drive_files_scanned=50, drive_files_saved=10,
                total_saved=10,
            )

        harvester.harvest_email = mock_gmail
        harvester.harvest_chat = mock_chat
        harvester.harvest_drive = mock_drive

        stats = harvester.harvest_all(
            user_email="alice@co.com",
            sources=["gmail", "chat", "drive"],
        )

        assert stats.user_email == "alice@co.com"
        assert stats.emails_scanned == 100
        assert stats.chat_messages_scanned == 200
        assert stats.drive_files_scanned == 50
        assert stats.total_saved == 55

    def test_harvest_all_single_source(self, tmp_path):
        """harvest_all with sources=['gmail'] should only harvest Gmail."""
        from threadweave.connectors.gws.harvest import OffboardingHarvester, HarvestStats
        from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials

        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')
        creds = GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@co.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        auth = GWSAuth(creds)

        harvester = OffboardingHarvester(
            auth, checkpoint_dir=str(tmp_path / "checkpoints"),
        )

        harvester.harvest_email = lambda email, max_messages=None, **kw: HarvestStats(
            user_email=email, emails_scanned=50, emails_saved=20, total_saved=20,
        )
        harvester.harvest_chat = lambda email, **kw: HarvestStats()
        harvester.harvest_drive = lambda email, max_files=None, **kw: HarvestStats()

        stats = harvester.harvest_all(
            user_email="bob@co.com",
            sources=["gmail"],
        )

        assert stats.emails_scanned == 50
        assert stats.chat_messages_scanned == 0
        assert stats.drive_files_scanned == 0

    def test_harvest_all_saves_report(self, tmp_path):
        """After harvest_all, a report should be available via generate_report."""
        from threadweave.connectors.gws.harvest import OffboardingHarvester, HarvestStats
        from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials

        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')
        auth = GWSAuth(GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@co.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ))

        harvester = OffboardingHarvester(
            auth, checkpoint_dir=str(tmp_path / "checkpoints"),
        )
        harvester.harvest_email = lambda email, max_messages=None, **kw: HarvestStats(
            user_email=email, emails_saved=42, total_saved=42,
        )

        harvester.harvest_all(user_email="carol@co.com", sources=["gmail"])
        report = harvester.generate_report("carol@co.com")

        assert report is not None
        assert report["emails_saved"] == 42


# ═══════════════════════════════════════════════════════════════════════
# Harvest Checkpoint Resume
# ═══════════════════════════════════════════════════════════════════════

class TestHarvestResume:
    def test_checkpoint_persists_between_harvests(self, tmp_path):
        """Checkpoint should survive between harvest_all calls."""
        from threadweave.connectors.gws.harvest import OffboardingHarvester
        from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials

        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')
        auth = GWSAuth(GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@co.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ))

        chk_dir = str(tmp_path / "checkpoints")
        harvester = OffboardingHarvester(auth, checkpoint_dir=chk_dir)

        harvester._save_checkpoint("alice@co.com", {"msg1", "msg2", "msg3"})

        harvester2 = OffboardingHarvester(auth, checkpoint_dir=chk_dir)
        seen = harvester2._load_checkpoint("alice@co.com")

        assert seen == {"msg1", "msg2", "msg3"}
        assert len(seen) == 3
