# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Shared fixtures for ThreadWeave connector tests.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Isolate the durable audit log from the real ~/.threadweave directory.
# Must be set before threadweave.confidentiality is imported.
_audit_tmp = tempfile.mkdtemp(prefix="threadweave-audit-test-")
os.environ["THREADWEAVE_AUDIT_DB"] = os.path.join(_audit_tmp, "audit.sqlite3")

# Isolate the durable entry store too (set before threadweave.api is imported)
_entry_tmp = tempfile.mkdtemp(prefix="threadweave-entry-test-")
os.environ["THREADWEAVE_ENTRY_DB"] = os.path.join(_entry_tmp, "entries.sqlite3")


@pytest.fixture
def sample_threadweave_entry():
    """A representative ThreadWeave entry as returned by the API."""
    return {
        "id": "abc123",
        "content": "We decided to use PostgreSQL for the new platform because of its JSONB support and mature ecosystem.",
        "wing": "engineering",
        "room": "database",
        "scope": "team",
        "source_type": "email",
        "author_id": "alice@company.com",
        "created_at": "2025-01-15T10:00:00",
        "entities": [{"type": "technology", "value": "PostgreSQL"}],
        "content_type": "decision",
        "has_pii": False,
        "tenant_id": "default",
        "sensitivity": "internal",
    }


@pytest.fixture
def sample_threadweave_entries():
    """A batch of entries for bulk sync testing."""
    return [
        {
            "id": "e1", "content": "Use Redis for caching session data.",
            "wing": "engineering", "room": "architecture",
            "content_type": "answer", "author_id": "bob",
            "source_type": "chat", "created_at": "2025-01-10T09:00:00",
            "scope": "team", "sensitivity": "internal",
        },
        {
            "id": "e2", "content": "We are deprecating the SOAP API by Q3.",
            "wing": "platform", "room": "api-design",
            "content_type": "decision", "author_id": "carol",
            "source_type": "email", "created_at": "2025-01-12T14:00:00",
            "scope": "organization", "sensitivity": "internal",
        },
        {
            "id": "e3", "content": "Client Acme Corp requires SOC2 compliance for all integrations.",
            "wing": "legal", "room": "compliance",
            "content_type": "reference", "author_id": "dave",
            "source_type": "sharepoint", "created_at": "2025-01-14T11:00:00",
            "scope": "department", "sensitivity": "client_confidential",
            "client_id": "acme-corp",
        },
    ]


@pytest.fixture
def tmp_credentials_file():
    """Create a temporary JSON credentials file and return its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"type": "service_account"}')
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def mock_gmail_api_response():
    """A minimal Gmail API message list response."""
    return {
        "messages": [
            {"id": "msg1", "threadId": "thread1"},
            {"id": "msg2", "threadId": "thread2"},
        ],
        "resultSizeEstimate": 2,
    }


@pytest.fixture
def mock_chat_api_response():
    """A minimal Google Chat API spaces list response."""
    return {
        "spaces": [
            {"name": "spaces/AAA", "displayName": "Engineering"},
            {"name": "spaces/BBB", "displayName": "Product"},
        ],
    }


@pytest.fixture
def mock_drive_api_response():
    """A minimal Google Drive API file list response."""
    return {
        "files": [
            {
                "id": "file1", "name": "Architecture Decisions.md",
                "mimeType": "text/markdown", "size": "2048",
                "parents": ["folder1"],
                "modifiedTime": "2025-01-15T10:00:00Z",
            },
            {
                "id": "file2", "name": "Budget 2025.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": "50000",
                "parents": ["folder2"],
                "modifiedTime": "2025-01-10T08:00:00Z",
            },
        ],
    }
