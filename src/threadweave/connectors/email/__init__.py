# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Email Connector for ThreadWeave.

Monitors Exchange Online mailboxes via Microsoft Graph API
for organizational knowledge in email threads.

Modes:
    shared_mailbox  - Monitor knowledge@firma.no (forward/CC target)
    personal        - Monitor specific user mailboxes
    webhook         - Graph API change notifications (push)
    polling         - Periodic delta queries (fallback)

Authentication:
    Client credentials (app-only) via Azure AD app registration.
    Required scopes: Mail.Read, Mail.ReadWrite (for marking processed)
"""

from threadweave.connectors.email.watcher import MailWatcher
from threadweave.connectors.email.processor import EmailProcessor

__all__ = ["MailWatcher", "EmailProcessor"]
