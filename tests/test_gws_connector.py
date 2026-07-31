# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for Google Workspace connector — auth, Gmail parser, Chat, Drive.
"""

import base64
import json
import os
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials
from threadweave.connectors.gws.gmail import GmailWatcher, GmailMessage
from threadweave.connectors.gws.chat import ChatListener, ChatMessage
from threadweave.connectors.gws.drive import DriveCrawler


# ═══════════════════════════════════════════════════════════════════════
# Auth Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGWSCredentials:
    def test_from_env_all_set(self, tmp_path):
        creds_file = tmp_path / "sa.json"
        creds_file.write_text('{"type": "service_account"}')

        with patch.dict(os.environ, {
            "THREADWEAVE_GWS_CREDENTIALS_PATH": str(creds_file),
            "THREADWEAVE_GWS_DELEGATED_ACCOUNT": "admin@company.com",
        }):
            creds = GWSCredentials.from_env()
            assert creds is not None
            assert creds.delegated_account == "admin@company.com"
            assert creds.is_configured()

    def test_from_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = GWSCredentials.from_env()
            assert creds is None

    def test_from_env_missing_file(self):
        with patch.dict(os.environ, {
            "THREADWEAVE_GWS_CREDENTIALS_PATH": "/nonexistent/file.json",
            "THREADWEAVE_GWS_DELEGATED_ACCOUNT": "admin@company.com",
        }):
            creds = GWSCredentials.from_env()
            # When the credentials file doesn't exist, from_env returns None
            # because the path check fails
            assert creds is None

    def test_is_configured(self, tmp_path):
        creds_file = tmp_path / "sa.json"
        creds_file.write_text("{}")
        creds = GWSCredentials(
            credentials_path=str(creds_file),
            delegated_account="admin@company.com",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        assert creds.is_configured()

    def test_custom_scopes(self, tmp_path):
        creds_file = tmp_path / "sa.json"
        creds_file.write_text("{}")
        with patch.dict(os.environ, {
            "THREADWEAVE_GWS_CREDENTIALS_PATH": str(creds_file),
            "THREADWEAVE_GWS_DELEGATED_ACCOUNT": "admin@company.com",
            "THREADWEAVE_GWS_SCOPES": "scope1,scope2",
        }):
            creds = GWSCredentials.from_env()
            assert creds.scopes == ["scope1", "scope2"]


# ═══════════════════════════════════════════════════════════════════════
# Gmail Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGmailMessageParsing:
    def _make_raw_email(self, subject: str, body: str, sender: str = "alice@company.com") -> str:
        """Create a base64-encoded raw email."""
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = "bob@company.com"
        msg["Date"] = "Mon, 15 Jan 2025 10:00:00 +0000"
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def test_parse_simple_message(self):
        raw = self._make_raw_email(
            "Architecture decision",
            "We decided to use Postgres for the new platform because of JSONB support.",
        )
        gmail_msg = {
            "id": "msg123",
            "threadId": "thread456",
            "raw": raw,
            "snippet": "We decided to use Postgres...",
        }

        watcher = GmailWatcher(auth=None)  # Auth not needed for parsing
        parsed = watcher._parse_message(gmail_msg)

        assert parsed is not None
        assert parsed.message_id == "msg123"
        assert parsed.thread_id == "thread456"
        assert "Postgres" in parsed.body
        assert "architecture decision" in parsed.subject.lower()

    def test_parse_short_message_skipped(self):
        raw = self._make_raw_email("Hi", "ok")
        gmail_msg = {"id": "msg1", "threadId": "t1", "raw": raw}
        watcher = GmailWatcher(auth=None)
        parsed = watcher._parse_message(gmail_msg)
        assert parsed is None  # Too short

    def test_extract_recipients(self):
        watcher = GmailWatcher(auth=None)
        headers = {
            "to": "alice@company.com, bob@company.com",
            "cc": "charlie@company.com",
        }
        recipients = watcher._extract_recipients(headers)
        assert len(recipients) == 3
        assert "alice@company.com" in recipients
        assert "bob@company.com" in recipients
        assert "charlie@company.com" in recipients

    def test_extract_body_from_html_email(self):
        """HTML emails should yield plain text from the text/plain part."""
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Architecture Decision"
        msg["From"] = "a@b.com"
        msg["To"] = "c@d.com"
        msg["Date"] = "Mon, 15 Jan 2025 10:00:00 +0000"
        msg.attach(MIMEText("<html><body><p>We chose Postgres</p></body></html>", "html"))
        msg.attach(MIMEText("We chose Postgres for its JSONB support.", "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        gmail_msg = {"id": "m1", "threadId": "t1", "raw": raw}
        watcher = GmailWatcher(auth=None)
        parsed = watcher._parse_message(gmail_msg)
        assert parsed is not None
        assert "Postgres" in parsed.body


class TestGmailSubmit:
    @patch("threadweave.connectors.gws.gmail.requests.post")
    def test_submit_saves(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"should_save": True, "id": "abc"}
        mock_post.return_value = mock_resp

        watcher = GmailWatcher(auth=None)
        msg = GmailMessage(
            message_id="m1", thread_id="t1",
            sender="a@b.com", recipients=["c@d.com"],
            subject="Important decision",
            body="We chose Kafka for event streaming.",
            snippet="We chose Kafka...",
            timestamp="2025-01-15T10:00:00",
        )
        stats = watcher.submit_messages([msg])
        assert stats["submitted"] == 1
        assert stats["saved"] == 1


# ═══════════════════════════════════════════════════════════════════════
# Chat Tests
# ═══════════════════════════════════════════════════════════════════════

class TestChatParsing:
    def test_parse_human_message(self):
        raw = {
            "name": "spaces/AAA/messages/msg1",
            "sender": {"displayName": "Alice", "type": "HUMAN"},
            "text": "We should use GraphQL for the new API endpoint because it reduces over-fetching.",
            "createTime": "2025-01-15T10:00:00Z",
            "thread": {"name": "spaces/AAA/threads/t1"},
        }
        listener = ChatListener(auth=None)
        parsed = listener._parse_message(raw, "spaces/AAA")
        assert parsed is not None
        assert parsed.message_id == "msg1"
        assert parsed.sender == "Alice"
        assert "GraphQL" in parsed.text

    def test_skip_bot_message(self):
        raw = {
            "name": "spaces/AAA/messages/msg2",
            "sender": {"displayName": "Slackbot", "type": "BOT"},
            "text": "Reminder: standup in 5 minutes.",
        }
        listener = ChatListener(auth=None)
        parsed = listener._parse_message(raw, "spaces/AAA")
        assert parsed is None  # Bots are skipped

    def test_skip_short_message(self):
        raw = {
            "name": "spaces/AAA/messages/msg3",
            "sender": {"displayName": "Bob", "type": "HUMAN"},
            "text": "ok",
        }
        listener = ChatListener(auth=None)
        parsed = listener._parse_message(raw, "spaces/AAA")
        assert parsed is None  # Too short

    def test_thread_reply_detection(self):
        raw = {
            "name": "spaces/AAA/messages/msg4",
            "sender": {"displayName": "Charlie", "type": "HUMAN"},
            "text": "That's a great point about the caching layer.",
            "thread": {"name": "spaces/AAA/threads/t1", "threadReply": True},
            "createTime": "2025-01-15T11:00:00Z",
        }
        listener = ChatListener(auth=None)
        parsed = listener._parse_message(raw, "spaces/AAA")
        assert parsed is not None
        assert parsed.is_thread_reply is True

    def test_new_message_detection(self):
        listener = ChatListener(auth=None)
        # No prior poll
        msg = ChatMessage(
            message_id="m1", space_id="s1", space_name="test",
            sender="Alice", sender_type="HUMAN",
            text="Test message",
            timestamp="2025-01-15T10:00:00",
        )
        assert listener._is_new(msg) is True

        # Set last poll
        listener._last_poll["s1"] = "2025-01-15T12:00:00"
        # Older message
        assert listener._is_new(msg) is False


# ═══════════════════════════════════════════════════════════════════════
# Drive Tests
# ═══════════════════════════════════════════════════════════════════════

class TestDriveCrawler:
    def test_supported_google_mime_types(self):
        """Verify we have exporters for all Google Docs types."""
        from threadweave.connectors.gws.drive import SUPPORTED_GOOGLE_MIMES
        assert "application/vnd.google-apps.document" in SUPPORTED_GOOGLE_MIMES
        assert "application/vnd.google-apps.spreadsheet" in SUPPORTED_GOOGLE_MIMES
        assert "application/vnd.google-apps.presentation" in SUPPORTED_GOOGLE_MIMES

    def test_supported_file_extensions(self):
        from threadweave.connectors.gws.drive import SUPPORTED_FILE_EXTENSIONS
        assert ".txt" in SUPPORTED_FILE_EXTENSIONS
        assert ".md" in SUPPORTED_FILE_EXTENSIONS
        assert ".py" in SUPPORTED_FILE_EXTENSIONS

    def test_process_file_too_large_skipped(self):
        crawler = DriveCrawler(auth=None)
        file_info = {
            "id": "f1", "name": "big.pdf", "mimeType": "application/pdf",
            "size": "50000000",  # 50 MB
        }
        result = crawler._process_file(None, file_info)
        assert result is None

    def test_process_file_short_content_skipped(self):
        crawler = DriveCrawler(auth=None)
        # Mock _extract_text to return short text
        with patch.object(crawler, "_extract_text", return_value="short"):
            file_info = {
                "id": "f1", "name": "test.txt", "mimeType": "text/plain",
                "size": "100",
            }
            result = crawler._process_file(None, file_info)
            assert result is None

    def test_process_file_duplicate_skipped(self):
        crawler = DriveCrawler(auth=None)
        with patch.object(crawler, "_extract_text", return_value="A" * 100):
            file_info = {
                "id": "f1", "name": "doc.txt", "mimeType": "text/plain",
                "size": "100",
            }
            # First time: processed
            result1 = crawler._process_file(None, file_info)
            assert result1 is not None

            # Second time: skipped (content hash dedup)
            result2 = crawler._process_file(None, file_info)
            assert result2 is None

    def test_wing_from_folder_mapping(self):
        crawler = DriveCrawler(
            auth=None,
            folder_mapping={"folder_eng": "engineering"},
        )
        with patch.object(crawler, "_extract_text", return_value="A" * 100):
            file_info = {
                "id": "f1", "name": "doc.txt", "mimeType": "text/plain",
                "size": "100", "parents": ["folder_eng"],
            }
            result = crawler._process_file(None, file_info)
            assert result is not None
            assert result["wing"] == "engineering"

    def test_wing_defaults_to_drive(self):
        crawler = DriveCrawler(auth=None)
        with patch.object(crawler, "_extract_text", return_value="A" * 100):
            file_info = {
                "id": "f1", "name": "doc.txt", "mimeType": "text/plain",
                "size": "100", "parents": ["unknown_folder"],
            }
            result = crawler._process_file(None, file_info)
            assert result is not None
            assert result["wing"] == "drive"  # Default


class TestDriveSubmit:
    @patch("threadweave.connectors.gws.drive.requests.post")
    def test_submit_documents(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"should_save": True, "id": "abc"}
        mock_post.return_value = mock_resp

        crawler = DriveCrawler(auth=None)
        docs = [
            {
                "id": "f1", "name": "Architecture Decision.md",
                "mime_type": "text/markdown",
                "text": "# We chose Postgres\n\nBecause JSONB.",
                "folder": "folder1", "wing": "engineering",
                "url": "https://drive.google.com/file/f1",
                "modified": "2025-01-15T10:00:00",
            },
        ]
        stats = crawler.submit_documents(docs)
        assert stats["submitted"] == 1
        assert stats["saved"] == 1
        mock_post.assert_called_once()
        call_args = mock_post.call_args[1]["json"]
        assert call_args["source"] == "google_drive"
        assert "Postgres" in call_args["content"]


# ═══════════════════════════════════════════════════════════════════════
# CLI Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCLI:
    def test_gws_help(self):
        """CLI should have a gws subcommand."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "gws", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "check" in result.stdout
        assert "sync" in result.stdout
        assert "watch" in result.stdout

    def test_gws_check_help(self):
        """gws check should have --host and --port options."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "gws", "check", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--host" in result.stdout
        assert "--port" in result.stdout

    def test_gws_sync_source_options(self):
        """gws sync should accept --source all/gmail/chat/drive."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "gws", "sync", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--source" in result.stdout
        assert "all" in result.stdout

    def test_gws_watch_interval_option(self):
        """gws watch should have --interval option."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "gws", "watch", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--interval" in result.stdout

    def test_gws_harvest_requires_email(self):
        """gws harvest should require --email."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "threadweave.cli", "gws", "harvest", "--help"],
            capture_output=True, text=True, timeout=5,
            cwd="C:/tmp/threadweave",
            env={**os.environ, "PYTHONPATH": "C:/tmp/threadweave/src"},
        )
        assert "--email" in result.stdout


# ═══════════════════════════════════════════════════════════════════════
# Gmail — fetch_recent / process_inbox Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGmailFetchRecent:
    def test_fetch_recent_returns_parsed_messages(self):
        """fetch_recent should call users.messages.list, get each message,
        and return parsed GmailMessage objects."""
        import base64
        from email.mime.text import MIMEText
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        msg = MIMEText("We decided to migrate to Kubernetes for all services.")
        msg["Subject"] = "Infrastructure Decision"
        msg["From"] = "alice@company.com"
        msg["To"] = "bob@company.com"
        msg["Date"] = "Mon, 15 Jan 2025 10:00:00 +0000"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        watcher = GmailWatcher(auth=None)

        # Mock the Gmail service via auth
        mock_service = MagicMock()
        mock_list = mock_service.users.return_value.messages.return_value.list
        mock_list.return_value.execute.return_value = {
            "messages": [{"id": "msg1", "threadId": "thread1"}],
        }
        mock_get = mock_service.users.return_value.messages.return_value.get
        mock_get.return_value.execute.return_value = {
            "id": "msg1",
            "threadId": "thread1",
            "raw": raw,
            "snippet": "We decided to migrate...",
        }
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        messages = watcher.fetch_recent(max_results=5)

        assert len(messages) == 1
        assert messages[0].message_id == "msg1"
        assert "Kubernetes" in messages[0].body

    def test_fetch_recent_empty_inbox(self):
        """fetch_recent should return empty list when no messages exist."""
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        watcher = GmailWatcher(auth=None)
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "resultSizeEstimate": 0,
        }
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        messages = watcher.fetch_recent(max_results=10)
        assert messages == []

    def test_fetch_recent_respects_query(self):
        """fetch_recent should pass the query parameter to the Gmail API."""
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        watcher = GmailWatcher(auth=None)
        mock_service = MagicMock()
        mock_list = mock_service.users.return_value.messages.return_value.list
        mock_list.return_value.execute.return_value = {"resultSizeEstimate": 0}
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        watcher.fetch_recent(max_results=10, query="subject:architecture")

        call_kwargs = mock_list.call_args[1]
        assert "architecture" in call_kwargs.get("q", "")

    def test_fetch_recent_api_error_returns_empty(self):
        """API errors from Gmail should not crash — return empty list."""
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        watcher = GmailWatcher(auth=None)
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.side_effect = \
            Exception("Rate limit exceeded")
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        messages = watcher.fetch_recent()
        assert messages == []

    def test_parse_message_missing_raw(self):
        """Messages without 'raw' field should be skipped."""
        from threadweave.connectors.gws.gmail import GmailWatcher

        watcher = GmailWatcher(auth=None)
        result = watcher._parse_message({"id": "m1", "threadId": "t1"})
        assert result is None


class TestGmailProcessInbox:
    @patch("threadweave.connectors.gws.gmail.requests.post")
    def test_process_inbox_submits_and_returns_stats(self, mock_post):
        """process_inbox should fetch messages, submit them, and return stats."""
        import base64
        from email.mime.text import MIMEText
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        msg = MIMEText("We chose gRPC for internal service communication.")
        msg["Subject"] = "API Architecture Decision"
        msg["From"] = "alice@company.com"
        msg["To"] = "team@company.com"
        msg["Date"] = "Mon, 15 Jan 2025 10:00:00 +0000"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        watcher = GmailWatcher(auth=None)
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m1", "threadId": "t1"}],
        }
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "raw": raw, "snippet": "...",
        }
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"should_save": True, "id": "abc"}
        mock_post.return_value = mock_resp

        stats = watcher.process_inbox(query="newer_than:1h")

        assert stats["submitted"] == 1
        assert stats["saved"] == 1
        assert stats["skipped"] == 0

    @patch("threadweave.connectors.gws.gmail.requests.post")
    def test_process_inbox_handles_api_error(self, mock_post):
        """process_inbox should count errors when ThreadWeave API is down."""
        import base64
        from email.mime.text import MIMEText
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.gmail import GmailWatcher

        msg = MIMEText("Test content that is long enough to be parsed correctly.")
        msg["Subject"] = "Test"
        msg["From"] = "a@b.com"
        msg["To"] = "c@d.com"
        msg["Date"] = "Mon, 15 Jan 2025 10:00:00 +0000"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        watcher = GmailWatcher(auth=None)
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m1", "threadId": "t1"}],
        }
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "raw": raw, "snippet": "...",
        }
        mock_auth = MagicMock()
        mock_auth.gmail.return_value = mock_service
        watcher.auth = mock_auth

        mock_post.side_effect = Exception("Connection refused")

        stats = watcher.process_inbox()

        assert stats["submitted"] == 1
        assert stats["errors"] >= 1


# ═══════════════════════════════════════════════════════════════════════
# Chat — submit_messages / process_all_spaces Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestChatSubmitMessages:
    @patch("threadweave.connectors.gws.chat.requests.post")
    def test_submit_messages_bulk(self, mock_post):
        """submit_messages should send multiple messages and aggregate stats."""
        from threadweave.connectors.gws.chat import ChatListener, ChatMessage

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"should_save": True, "id": "abc"}
        mock_post.return_value = mock_resp

        listener = ChatListener(auth=None)
        messages = [
            ChatMessage("m1", "s1", "Engineering", "Alice", "HUMAN",
                        "We should use GraphQL.", "2025-01-15T10:00:00"),
            ChatMessage("m2", "s1", "Engineering", "Bob", "HUMAN",
                        "Agreed, it reduces over-fetching significantly.", "2025-01-15T10:01:00"),
        ]

        stats = listener.submit_messages(messages)

        assert stats["submitted"] == 2
        assert stats["saved"] == 2

    @patch("threadweave.connectors.gws.chat.requests.post")
    def test_submit_messages_mixed_results(self, mock_post):
        """Some messages save, some don't — stats should reflect that."""
        from threadweave.connectors.gws.chat import ChatListener, ChatMessage

        mock_resp_saved = MagicMock(status_code=201)
        mock_resp_saved.json.return_value = {"should_save": True, "id": "x"}
        mock_resp_skipped = MagicMock(status_code=201)
        mock_resp_skipped.json.return_value = {"should_save": False}
        mock_post.side_effect = [mock_resp_saved, mock_resp_skipped]

        listener = ChatListener(auth=None)
        messages = [
            ChatMessage("m1", "s1", "Eng", "Alice", "HUMAN",
                        "Decision: use Postgres for everything.", "2025-01-15T10:00:00"),
            ChatMessage("m2", "s1", "Eng", "Bob", "HUMAN",
                        "ok", "2025-01-15T10:01:00"),
        ]

        stats = listener.submit_messages(messages)
        assert stats["submitted"] == 2
        assert stats["saved"] == 1
        assert stats["skipped"] == 1


class TestChatProcessAllSpaces:
    @patch("threadweave.connectors.gws.chat.requests.post")
    def test_process_all_spaces(self, mock_post):
        """process_all_spaces should list spaces, fetch messages from each, and submit."""
        from unittest.mock import MagicMock
        from threadweave.connectors.gws.chat import ChatListener

        mock_resp = MagicMock(status_code=201)
        mock_resp.json.return_value = {"should_save": True, "id": "x"}
        mock_post.return_value = mock_resp

        listener = ChatListener(auth=None)

        # Mock the Chat service via auth
        mock_service = MagicMock()
        mock_service.spaces.return_value.list.return_value.execute.return_value = {
            "spaces": [
                {"name": "spaces/AAA", "displayName": "Engineering"},
            ],
        }
        mock_service.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [
                {
                    "name": "spaces/AAA/messages/m1",
                    "sender": {"displayName": "Alice", "type": "HUMAN"},
                    "text": "We should adopt Terraform for all infrastructure.",
                    "createTime": "2025-01-15T10:00:00Z",
                    "thread": {"name": "spaces/AAA/threads/t1"},
                },
            ],
        }
        mock_auth = MagicMock()
        mock_auth.chat.return_value = mock_service
        listener.auth = mock_auth

        stats = listener.process_all_spaces()

        assert stats["submitted"] >= 1
        assert stats["saved"] >= 1


# ═══════════════════════════════════════════════════════════════════════
# Drive — crawl / process_drive End-to-End Tests
# ═══════════════════════════════════════════════════════════════════════

class TestDriveCrawlerEndToEnd:
    @patch("threadweave.connectors.gws.drive.requests.post")
    def test_crawl_submits_files(self, mock_post):
        """crawl should list files, extract text, and submit to ThreadWeave."""
        from unittest.mock import patch, MagicMock
        from threadweave.connectors.gws.drive import DriveCrawler

        mock_resp = MagicMock(status_code=201)
        mock_resp.json.return_value = {"should_save": True, "id": "x"}
        mock_post.return_value = mock_resp

        crawler = DriveCrawler(auth=None)
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "f1", "name": "Architecture Decision.md",
                    "mimeType": "text/markdown", "size": "2048",
                    "parents": ["folder1"],
                    "modifiedTime": "2025-01-15T10:00:00Z",
                },
            ],
        }
        mock_auth = MagicMock()
        mock_auth.drive.return_value = mock_service
        crawler.auth = mock_auth

        with patch.object(crawler, "_extract_text", return_value="# We chose Postgres\n\nBecause JSONB support is excellent."):
            results = crawler.crawl(max_results=10)

        assert len(results) >= 1
        assert results[0]["wing"] == "drive"

    @patch("threadweave.connectors.gws.drive.requests.post")
    def test_process_drive_returns_stats(self, mock_post):
        """process_drive should crawl and return aggregate stats."""
        from unittest.mock import patch, MagicMock
        from threadweave.connectors.gws.drive import DriveCrawler

        mock_resp = MagicMock(status_code=201)
        mock_resp.json.return_value = {"should_save": True, "id": "x"}
        mock_post.return_value = mock_resp

        crawler = DriveCrawler(auth=None)
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "f1", "name": "doc.md", "mimeType": "text/markdown",
                 "size": "1000", "parents": ["f1"], "modifiedTime": "2025-01-15T10:00:00Z"},
                {"id": "f2", "name": "notes.txt", "mimeType": "text/plain",
                 "size": "2000", "parents": ["f2"], "modifiedTime": "2025-01-14T10:00:00Z"},
            ],
        }
        mock_auth = MagicMock()
        mock_auth.drive.return_value = mock_service
        crawler.auth = mock_auth

        with patch.object(crawler, "_extract_text", side_effect=["Doc1-" + "A" * 200, "Doc2-" + "B" * 200]):
            stats = crawler.process_drive()

        assert stats["submitted"] == 2
        assert stats["saved"] == 2
