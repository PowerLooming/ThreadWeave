# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for the Microsoft Graph connector.
"""

import json
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from threadweave.connectors.graph.auth import (
    GraphAuth,
    GraphCredentials,
    GraphToken,
)
from threadweave.connectors.graph.schema import (
    CONNECTION_ID,
    CONNECTION_NAME,
    GraphExternalItem,
    GraphItemProperties,
    GraphAclEntry,
    map_threadweave_to_graph,
)
from threadweave.connectors.graph.connector import (
    ThreadWeaveGraphConnector,
    SyncStats,
)


# ── Auth Tests ─────────────────────────────────────────────────────

class TestGraphCredentials:
    def test_from_env_all_set(self):
        with patch.dict(os.environ, {
            "THREADWEAVE_GRAPH_TENANT_ID": "tenant-123",
            "THREADWEAVE_GRAPH_CLIENT_ID": "client-456",
            "THREADWEAVE_GRAPH_CLIENT_SECRET": "secret-789",
        }):
            creds = GraphCredentials.from_env()
            assert creds is not None
            assert creds.tenant_id == "tenant-123"
            assert creds.client_id == "client-456"
            assert creds.client_secret == "secret-789"
            assert creds.is_configured()

    def test_from_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = GraphCredentials.from_env()
            assert creds is None

    def test_from_env_partial(self):
        with patch.dict(os.environ, {
            "THREADWEAVE_GRAPH_TENANT_ID": "tenant-123",
        }):
            creds = GraphCredentials.from_env()
            assert creds is None  # Missing client_id and client_secret

    def test_is_configured(self):
        creds = GraphCredentials("t", "c", "s")
        assert creds.is_configured()

        empty = GraphCredentials("", "", "")
        assert not empty.is_configured()


class TestGraphAuth:
    def test_token_caching(self):
        creds = GraphCredentials("tenant", "client", "secret")
        auth = GraphAuth(creds)

        with patch.object(auth, "_acquire_token") as mock_acquire:
            mock_token = GraphToken(access_token="test-token", expires_at=9999999999)
            mock_acquire.return_value = mock_token

            token1 = auth.access_token
            token2 = auth.access_token

            assert token1 == "test-token"
            assert token2 == "test-token"
            # Should only call acquire once (cached)
            assert mock_acquire.call_count == 1

    def test_token_refresh_on_expiry(self):
        creds = GraphCredentials("tenant", "client", "secret")
        auth = GraphAuth(creds)

        # Directly test that _is_expired detects an expired token
        auth._token = GraphToken(access_token="old", expires_at=0)
        assert auth._is_expired() is True

        # And that a far-future token is NOT expired
        auth._token = GraphToken(access_token="fresh", expires_at=9999999999)
        assert auth._is_expired() is False

    @patch("threadweave.connectors.graph.auth.requests.post")
    def test_acquire_token(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "real-token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_resp

        creds = GraphCredentials("tenant", "client", "secret")
        auth = GraphAuth(creds)
        token = auth._acquire_token()

        assert token.access_token == "real-token"
        assert token.expires_at > 0

    def test_invalidate(self):
        creds = GraphCredentials("tenant", "client", "secret")
        auth = GraphAuth(creds)

        with patch.object(auth, "_acquire_token") as mock_acquire:
            mock_acquire.return_value = GraphToken("t1", 9999999999)
            _ = auth.access_token
            assert mock_acquire.call_count == 1

            auth.invalidate()
            mock_acquire.return_value = GraphToken("t2", 9999999999)
            _ = auth.access_token
            assert mock_acquire.call_count == 2


# ── Schema Tests ───────────────────────────────────────────────────

class TestGraphSchema:
    def test_connection_id(self):
        assert CONNECTION_ID == "threadweave"
        assert CONNECTION_NAME == "ThreadWeave Organizational Memory"

    def test_map_entry_to_graph_basic(self):
        entry = {
            "id": "abc123",
            "content": "We use Postgres for JSONB support.",
            "wing": "engineering",
            "room": "database",
            "content_type": "answer",
            "author_id": "alice",
            "source_type": "email",
            "created_at": "2025-01-15T10:00:00",
            "scope": "team",
        }

        item = map_threadweave_to_graph(entry)

        assert item.item_id == "abc123"
        assert item.properties.title is not None
        assert item.properties.wing == "engineering"
        assert item.properties.room == "database"
        assert item.properties.contentType == "answer"
        assert item.properties.author == "alice"
        assert len(item.acl) == 1
        assert item.acl[0].type == "everyone"  # Default when no wing→group mapping

    def test_map_entry_with_wing_to_group(self):
        entry = {
            "id": "abc123",
            "content": "Test content.",
            "wing": "engineering",
            "room": "test",
            "content_type": "decision",
            "author_id": "bob",
            "source_type": "manual",
            "created_at": "2025-01-15T10:00:00",
            "scope": "team",
        }
        wing_to_group = {"engineering": "group-eng-123"}

        item = map_threadweave_to_graph(entry, wing_to_group=wing_to_group)

        assert len(item.acl) == 1
        assert item.acl[0].type == "group"
        assert item.acl[0].value == "group-eng-123"

    def test_map_entry_auto_title(self):
        entry = {
            "id": "xyz",
            "content": "Always run integration tests before deploying.",
            "wing": "devops",
            "room": "ci",
            "content_type": "answer",
            "author_id": "ops",
            "source_type": "slack",
            "created_at": "2025-01-15T10:00:00",
            "scope": "team",
        }

        item = map_threadweave_to_graph(entry)
        assert item.properties.title == "Always run integration tests before deploying"

    def test_payload_generation(self):
        entry = {
            "id": "test1",
            "content": "Test content for payload.",
            "wing": "legal",
            "room": "contracts",
            "content_type": "decision",
            "author_id": "lawyer1",
            "source_type": "email",
            "created_at": "2025-03-01T09:00:00",
            "scope": "department",
        }

        item = map_threadweave_to_graph(entry)
        payload = item.to_payload()

        assert payload["id"] == "test1"
        assert payload["properties"]["wing"] == "legal"
        assert payload["properties"]["room"] == "contracts"
        assert payload["content"]["type"] == "text"
        assert payload["content"]["value"] == "Test content for payload."
        assert len(payload["acl"]) >= 1

    def test_acl_entry(self):
        acl = GraphAclEntry(accessType="grant", type="group", value="grp-123")
        d = acl.to_dict()
        assert d == {"accessType": "grant", "type": "group", "value": "grp-123"}

    def test_acl_entry_everyone(self):
        acl = GraphAclEntry(accessType="grant", type="everyone", value="")
        d = acl.to_dict()
        assert d["type"] == "everyone"


# ── Connector Tests ────────────────────────────────────────────────

class TestConnectorBasics:
    def test_constructor_defaults(self):
        connector = ThreadWeaveGraphConnector()
        assert connector.threadweave_url == "http://localhost:8000"
        assert connector._wing_to_group == {}

    def test_constructor_custom_url(self):
        connector = ThreadWeaveGraphConnector(
            threadweave_url="https://tw.company.com",
        )
        assert connector.threadweave_url == "https://tw.company.com"

    def test_not_configured_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            connector = ThreadWeaveGraphConnector()
            assert not connector.is_configured

    def test_configured_with_creds(self):
        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        assert connector.is_configured

    def test_configured_from_env(self):
        with patch.dict(os.environ, {
            "THREADWEAVE_GRAPH_TENANT_ID": "t",
            "THREADWEAVE_GRAPH_CLIENT_ID": "c",
            "THREADWEAVE_GRAPH_CLIENT_SECRET": "s",
        }):
            connector = ThreadWeaveGraphConnector()
            assert connector.is_configured

    def test_auth_raises_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            connector = ThreadWeaveGraphConnector()
            with pytest.raises(RuntimeError, match="credentials not configured"):
                _ = connector.auth

    def test_connection_endpoint(self):
        connector = ThreadWeaveGraphConnector()
        expected = "https://graph.microsoft.com/v1.0/external/connections/threadweave"
        assert connector.connection_endpoint == expected


class TestSyncStats:
    def test_empty_stats(self):
        s = SyncStats()
        assert s.success_rate == 1.0
        assert s.total_entries == 0

    def test_success_rate(self):
        s = SyncStats(created=95, failed=5)
        # 95/100 = 0.95 (but with updated=0, deleted=0: 95/95 = 1.0)
        # Created=95, failed=5 means attempted=100, success=(100-5)/100
        assert round(s.success_rate, 2) == 0.95

    def test_success_rate_all_failed(self):
        s = SyncStats(created=0, updated=0, failed=10)
        # 0 attempted, default to 1.0
        assert s.success_rate == 1.0

    def test_to_dict(self):
        s = SyncStats(
            total_entries=100, created=90, updated=5, failed=5,
            started_at="2025-01-01T00:00:00",
            completed_at="2025-01-01T00:05:00",
        )
        d = s.to_dict()
        assert d["total_entries"] == 100
        assert d["created"] == 90
        assert d["failed"] == 5
        assert round(d["success_rate"], 2) == 0.95


# ── Connector API mock tests ───────────────────────────────────────

class TestUpsertItem:
    @patch("threadweave.connectors.graph.connector.requests.put")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_upsert_success(self, mock_post, mock_put):
        """Test that upsert creates an item successfully."""
        # Mock the auth token acquisition
        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {
            "access_token": "test-token", "expires_in": 3600,
        }
        mock_post.return_value = mock_token_resp

        # Mock the Graph API PUT
        mock_put_resp = MagicMock()
        mock_put_resp.status_code = 201
        mock_put.return_value = mock_put_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        entry = {
            "id": "test-1",
            "content": "We chose Postgres for its JSONB support.",
            "wing": "engineering",
            "room": "database",
            "content_type": "decision",
            "author_id": "alice",
            "source_type": "email",
            "created_at": "2025-01-15T10:00:00",
            "scope": "team",
        }

        result = connector.upsert_item(entry)
        assert result is True
        mock_put.assert_called_once()

    @patch("threadweave.connectors.graph.connector.requests.put")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_upsert_failure(self, mock_post, mock_put):
        """Test that upsert returns False on API error."""
        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = {
            "access_token": "test-token", "expires_in": 3600,
        }
        mock_post.return_value = mock_token_resp

        mock_put_resp = MagicMock()
        mock_put_resp.status_code = 401
        mock_put_resp.text = "Unauthorized"
        mock_put.return_value = mock_put_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        entry = {"id": "test-1", "content": "test", "wing": "eng",
                 "room": "test", "content_type": "chat", "author_id": "a",
                 "source_type": "manual", "created_at": "", "scope": "team"}

        result = connector.upsert_item(entry)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# Full Sync Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestFullSyncIntegration:
    @patch("threadweave.connectors.graph.connector.requests.put")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_full_sync_creates_items(self, mock_post, mock_put):
        """Full sync should fetch entries from ThreadWeave and push each to Graph."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        # Auth token
        mock_token = MagicMock()
        mock_token.json.return_value = {
            "access_token": "tok", "expires_in": 3600,
        }
        mock_post.return_value = mock_token

        # Graph API PUT response
        mock_put_resp = MagicMock(status_code=201)
        mock_put.return_value = mock_put_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
            threadweave_url="http://localhost:8000",
        )

        # Replace _fetch_all_entries to return test data
        entry = {
            "id": "e1",
            "content": "We chose Postgres for its JSONB support.",
            "wing": "engineering",
            "room": "database",
            "scope": "team",
            "source_type": "email",
            "author_id": "alice@company.com",
            "created_at": "2025-01-15T10:00:00",
            "content_type": "decision",
        }
        connector._fetch_all_entries = lambda: [entry]

        stats = connector.full_sync()

        assert stats.total_entries == 1
        assert stats.created == 1
        assert stats.failed == 0

    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_full_sync_empty_threadweave(self, mock_post):
        """Full sync with no entries should return zero stats, no Graph calls."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {
            "access_token": "tok", "expires_in": 3600,
        }
        mock_post.return_value = mock_token

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        connector._fetch_all_entries = lambda: []

        stats = connector.full_sync()
        assert stats.total_entries == 0
        assert stats.created == 0


# ═══════════════════════════════════════════════════════════════════════
# Register Schema Edge Cases
# ═══════════════════════════════════════════════════════════════════════

class TestRegisterSchema:
    @patch("threadweave.connectors.graph.connector.requests.patch")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_register_schema_already_exists_triggers_patch(self, mock_post, mock_patch):
        """409 Conflict should trigger a PATCH update of the existing connection."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}

        mock_post.side_effect = [
            mock_token,
            MagicMock(status_code=409),
        ]
        mock_patch_resp = MagicMock(status_code=200)
        mock_patch.return_value = mock_patch_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        result = connector.register_schema()
        assert result is True
        mock_patch.assert_called_once()

    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_register_schema_failure(self, mock_post):
        """Non-409 errors should return False."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.side_effect = [
            mock_token,
            MagicMock(status_code=403, text="Forbidden"),
        ]

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        result = connector.register_schema()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# Delete Operations
# ═══════════════════════════════════════════════════════════════════════

class TestDeleteOperations:
    @patch("threadweave.connectors.graph.connector.requests.delete")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_delete_connection(self, mock_post, mock_delete):
        """delete_connection should return True when Graph accepts the deletion."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_delete_resp = MagicMock(status_code=202)
        mock_delete.return_value = mock_delete_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        assert connector.delete_connection() is True

    @patch("threadweave.connectors.graph.connector.requests.delete")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_delete_connection_failure(self, mock_post, mock_delete):
        """Non-202 status should return False."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_delete_resp = MagicMock(status_code=404)
        mock_delete.return_value = mock_delete_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        assert connector.delete_connection() is False

    @patch("threadweave.connectors.graph.connector.requests.delete")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_delete_item(self, mock_post, mock_delete):
        """delete_item should return True on 200/204."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_delete_resp = MagicMock(status_code=204)
        mock_delete.return_value = mock_delete_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        assert connector.delete_item("item-1") is True

    @patch("threadweave.connectors.graph.connector.requests.delete")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_delete_item_failure(self, mock_post, mock_delete):
        """delete_item should return False on non-2xx."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_delete_resp = MagicMock(status_code=500)
        mock_delete.return_value = mock_delete_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        assert connector.delete_item("item-1") is False


# ═══════════════════════════════════════════════════════════════════════
# Connection Status
# ═══════════════════════════════════════════════════════════════════════

class TestConnectionStatus:
    @patch("threadweave.connectors.graph.connector.requests.get")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_get_connection_status_success(self, mock_post, mock_get):
        """Should return the JSON response dict."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_get_resp = MagicMock(status_code=200)
        mock_get_resp.json.return_value = {
            "id": "threadweave", "name": "ThreadWeave", "state": "ready",
        }
        mock_get.return_value = mock_get_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        status = connector.get_connection_status()
        assert status is not None
        assert status["state"] == "ready"

    @patch("threadweave.connectors.graph.connector.requests.get")
    @patch("threadweave.connectors.graph.connector.requests.post")
    def test_get_connection_status_not_found(self, mock_post, mock_get):
        """404 should return None."""
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        mock_token = MagicMock()
        mock_token.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_post.return_value = mock_token

        mock_get_resp = MagicMock(status_code=404)
        mock_get.return_value = mock_get_resp

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        status = connector.get_connection_status()
        assert status is None


# ═══════════════════════════════════════════════════════════════════════
# SyncEngine Tests
# ═══════════════════════════════════════════════════════════════════════

class TestSyncEngine:
    def test_full_sync_updates_state(self, tmp_path):
        """full_sync should update SyncState and persist to disk."""
        from threadweave.connectors.graph.sync import SyncEngine, SyncState
        from threadweave.connectors.graph.connector import (
            ThreadWeaveGraphConnector, SyncStats,
        )

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        stats = SyncStats(total_entries=10, created=9, failed=1)
        stats.started_at = "2025-01-01T00:00:00"
        stats.completed_at = "2025-01-01T00:05:00"
        connector.full_sync = lambda: stats

        state_file = str(tmp_path / "sync_state.json")
        engine = SyncEngine(connector, state_file=state_file)

        result = engine.full_sync()

        assert result.total_entries == 10
        assert engine.state.items_synced == 9
        assert engine.state.total_failures == 1
        assert engine.state.last_full_sync != ""

        loaded = SyncState.load(state_file)
        assert loaded.items_synced == 9

    def test_incremental_sync_uses_last_timestamp(self, tmp_path):
        """incremental_sync should pass last_incremental_sync to connector."""
        from threadweave.connectors.graph.sync import SyncEngine, SyncState
        from threadweave.connectors.graph.connector import (
            ThreadWeaveGraphConnector, SyncStats,
        )

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )

        call_args = {}

        def mock_incremental(since=None):
            call_args["since"] = since
            return SyncStats(total_entries=2, updated=2)

        connector.incremental_sync = mock_incremental

        state_file = str(tmp_path / "sync_state.json")
        engine = SyncEngine(connector, state_file=state_file)

        engine.state.last_incremental_sync = "2025-01-15T10:00:00"
        engine.state.save(state_file)

        result = engine.incremental_sync()

        assert result.updated == 2
        assert call_args["since"] == "2025-01-15T10:00:00"

    def test_schema_setup_delegates(self, tmp_path):
        """schema_setup should delegate to connector.register_schema."""
        from threadweave.connectors.graph.sync import SyncEngine
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        connector.register_schema = lambda: True

        engine = SyncEngine(connector, state_file=str(tmp_path / "state.json"))
        assert engine.schema_setup() is True

    def test_status_includes_all_info(self, tmp_path):
        """status() should return a comprehensive dict."""
        from threadweave.connectors.graph.sync import SyncEngine
        from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

        connector = ThreadWeaveGraphConnector(
            tenant_id="t", client_id="c", client_secret="s",
        )
        connector.get_connection_status = lambda: {"state": "ready"}

        engine = SyncEngine(connector, state_file=str(tmp_path / "state.json"))
        status = engine.status()

        assert "state" in status
        assert "connection" in status
        assert "graph_configured" in status
        assert status["graph_configured"] is True
        assert status["running"] is False


# ═══════════════════════════════════════════════════════════════════════
# Graph CLI Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGraphCLI:
    def test_graph_help(self):
        """CLI should have a graph subcommand."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "graph", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "setup" in result.stdout
        assert "sync" in result.stdout
        assert "status" in result.stdout
        assert "daemon" in result.stdout

    def test_graph_setup_help(self):
        """graph setup should have --host and --port."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "graph", "setup", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--host" in result.stdout
        assert "--port" in result.stdout

    def test_graph_sync_help(self):
        """graph sync should have --host and --port."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "graph", "sync", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--host" in result.stdout

    def test_graph_daemon_help(self):
        """graph daemon should have --interval."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "graph", "daemon", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--interval" in result.stdout
