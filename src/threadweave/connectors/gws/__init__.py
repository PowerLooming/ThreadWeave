# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Google Workspace Connector for ThreadWeave.

Captures organizational knowledge from Google Workspace:
- Gmail: inbox messages → knowledge entries
- Google Chat: space messages → knowledge entries
- Google Drive: documents → knowledge entries

All connectors use the same ingestion pipeline:
    Source → Extract text → POST /api/v1/ingest → Detect → Save

Quick Start:
    1. Create a Google Cloud service account
    2. Enable Gmail, Chat, and Drive APIs in the Google Cloud Console
    3. Grant domain-wide delegation in Google Admin Console
    4. Set environment variables:
       THREADWEAVE_GWS_CREDENTIALS_PATH=/path/to/service-account-key.json
       THREADWEAVE_GWS_DELEGATED_ACCOUNT=admin@company.com
    5. Run: threadweave gws check     (verify connectivity)
    6. Run: threadweave gws sync      (one-shot sync)
    7. Run: threadweave gws watch     (continuous polling)
"""

from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials
from threadweave.connectors.gws.gmail import GmailWatcher, GmailMessage
from threadweave.connectors.gws.chat import ChatListener, ChatMessage
from threadweave.connectors.gws.drive import DriveCrawler
from threadweave.connectors.gws.harvest import (
    OffboardingHarvester,
    HarvestStats,
    generate_onboarding_brief,
)

__all__ = [
    "GWSAuth",
    "GWSCredentials",
    "GmailWatcher",
    "GmailMessage",
    "ChatListener",
    "ChatMessage",
    "DriveCrawler",
    "OffboardingHarvester",
    "HarvestStats",
    "generate_onboarding_brief",
]
