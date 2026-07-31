# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Graph Auth — Azure AD client credentials authentication for Microsoft Graph.

Uses app-only OAuth2 client credentials flow. No user interaction needed.
The app registration must have ExternalConnection.ReadWrite.OwnedBy or
ExternalItem.ReadWrite.OwnedBy permission.

Configuration via environment variables or direct constructor args:
    THREADWEAVE_GRAPH_TENANT_ID   — Azure AD tenant ID
    THREADWEAVE_GRAPH_CLIENT_ID  — App registration client ID
    THREADWEAVE_GRAPH_CLIENT_SECRET — App registration client secret
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("threadweave.graph.auth")

# OAuth2 token endpoint
# OAuth2 token endpoint
_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
# Default scopes for Microsoft Graph
_DEFAULT_SCOPES = ["https://graph.microsoft.com/.default"]


@dataclass
class GraphCredentials:
    """Azure AD app-only credentials."""
    tenant_id: str
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> Optional["GraphCredentials"]:
        """Load credentials from environment variables."""
        tenant_id = os.environ.get("THREADWEAVE_GRAPH_TENANT_ID", "")
        client_id = os.environ.get("THREADWEAVE_GRAPH_CLIENT_ID", "")
        client_secret = os.environ.get("THREADWEAVE_GRAPH_CLIENT_SECRET", "")
        if not all([tenant_id, client_id, client_secret]):
            return None
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    def is_configured(self) -> bool:
        """Check if all credential fields are non-empty."""
        return bool(self.tenant_id and self.client_id and self.client_secret)


@dataclass
class GraphToken:
    """Cached access token with expiry."""
    access_token: str
    expires_at: float  # Unix timestamp


class GraphAuth:
    """Authenticate with Microsoft Graph using client credentials flow.

    Caches the access token and refreshes it before expiry.
    """

    def __init__(self, credentials: GraphCredentials):
        self.credentials = credentials
        self._token: Optional[GraphToken] = None

    @property
    def access_token(self) -> str:
        """Get a valid access token, refreshing if necessary."""
        if self._token is None or self._is_expired():
            self._token = self._acquire_token()
        return self._token.access_token

    def _is_expired(self) -> bool:
        """Check if the cached token is expired or about to expire."""
        if self._token is None:
            return True
        # Refresh 60 seconds before actual expiry
        return time.time() + 60 >= self._token.expires_at

    def _acquire_token(self) -> GraphToken:
        """Acquire a new access token from Azure AD."""
        url = _TOKEN_URL.format(tenant_id=self.credentials.tenant_id)
        payload = {
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "scope": " ".join(_DEFAULT_SCOPES),
            "grant_type": "client_credentials",
        }

        resp = requests.post(url, data=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        expires_at = time.time() + expires_in

        logger.debug(
            "Acquired Graph token, expires in %ds", expires_in,
        )
        return GraphToken(access_token=access_token, expires_at=expires_at)

    def invalidate(self) -> None:
        """Force a token refresh on next access."""
        self._token = None