# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
SharePoint Connector for ThreadWeave.

Watches SharePoint document libraries for new and changed documents,
mines them into MemPalace, and maps SharePoint structure to
organizational memory (sites -> wings, libraries -> rooms).

Modes:
    polling - Periodic delta queries (the shipped capture path)

Authentication:
    Client credentials (app-only) via Azure AD app registration.
    Required scopes: Sites.Read.All, Files.Read.All
"""

from threadweave.connectors.sharepoint.watcher import GraphClient
from threadweave.connectors.sharepoint.processor import DocumentProcessor

__all__ = ["GraphClient", "DocumentProcessor"]
