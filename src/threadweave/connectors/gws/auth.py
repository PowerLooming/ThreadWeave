# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
GWS Auth — Google Workspace service account authentication.

Uses a Google Cloud service account with domain-wide delegation.
The GWS admin grants the service account access to specific scopes
via the Google Admin console (Security → API Controls → Domain-wide Delegation).

Configuration via environment variables or constructor args:
    THREADWEAVE_GWS_CREDENTIALS_PATH — path to service account JSON key file
    THREADWEAVE_GWS_DELEGATED_ACCOUNT — email of the admin/user to impersonate
    THREADWEAVE_GWS_SCOPES — comma-separated scope overrides (optional)

Required scopes (auto-requested):
    - https://www.googleapis.com/auth/gmail.readonly
    - https://www.googleapis.com/auth/chat.messages.readonly
    - https://www.googleapis.com/auth/drive.readonly
    - https://www.googleapis.com/auth/admin.reports.audit.readonly
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import Resource

logger = logging.getLogger("threadweave.gws.auth")

# Default scopes needed for knowledge capture
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


@dataclass
class GWSCredentials:
    """Google Workspace service account credentials."""

    credentials_path: str  # Path to service account JSON key
    delegated_account: str  # Email of the user to impersonate
    scopes: list[str]

    @classmethod
    def from_env(cls) -> Optional["GWSCredentials"]:
        """Load credentials from environment variables."""
        creds_path = os.environ.get(
            "THREADWEAVE_GWS_CREDENTIALS_PATH",
            os.path.expanduser("~/.threadweave/gws_service_account.json"),
        )
        delegated = os.environ.get("THREADWEAVE_GWS_DELEGATED_ACCOUNT", "")

        if not delegated or not Path(creds_path).is_file():
            return None

        scopes_env = os.environ.get("THREADWEAVE_GWS_SCOPES", "")
        scopes = scopes_env.split(",") if scopes_env else DEFAULT_SCOPES

        return cls(
            credentials_path=creds_path,
            delegated_account=delegated,
            scopes=scopes,
        )

    def is_configured(self) -> bool:
        """Check if all required fields are set."""
        return bool(
            self.credentials_path
            and Path(self.credentials_path).is_file()
            and self.delegated_account
        )


class GWSAuth:
    """Authenticate with Google Workspace APIs using a service account.

    Caches the credentials and builds API service objects on demand.
    """

    def __init__(self, credentials: GWSCredentials):
        self._credentials = credentials
        self._creds = None  # type: ignore[assignment]

    @property
    def creds(self):
        """Get or build the delegated credentials."""
        if self._creds is None:
            from google.oauth2 import service_account

            self._creds = service_account.Credentials.from_service_account_file(
                self._credentials.credentials_path,
                scopes=self._credentials.scopes,
            )
            # Domain-wide delegation: impersonate the specified user
            self._creds = self._creds.with_subject(
                self._credentials.delegated_account,
            )
            logger.debug(
                "Built delegated credentials for %s",
                self._credentials.delegated_account,
            )
        return self._creds

    def gmail(self):
        """Build an authorized Gmail API v1 service."""
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=self.creds)

    def chat(self):
        """Build an authorized Google Chat API v1 service."""
        from googleapiclient.discovery import build
        return build("chat", "v1", credentials=self.creds)

    def drive(self):
        """Build an authorized Drive API v3 service."""
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=self.creds)

    def refresh(self) -> None:
        """Force credential refresh on next use."""
        self._creds = None
