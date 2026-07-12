# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
SharePoint Connector for ThreadWeave.

Watches SharePoint document libraries for new and changed documents,
mines them into MemPalace, and maps SharePoint structure to
organizational memory (sites -> wings, libraries -> rooms).

Modes:
    webhook - Microsoft Graph change notifications (push)
    polling - Periodic delta queries (fallback for on-prem/restricted)

Authentication:
    Client credentials (app-only) via Azure AD app registration.
    Required scopes: Sites.Read.All, Files.Read.All
"""

from threadweave.connectors.sharepoint.watcher import (
    GraphClient,
    WebhookManager,
)
from threadweave.connectors.sharepoint.processor import DocumentProcessor

__all__ = ["GraphClient", "WebhookManager", "DocumentProcessor"]
