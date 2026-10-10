# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
SharePoint Watcher: Microsoft Graph API client for document capture.

Handles:
    - Azure AD authentication (client credentials)
    - Site and drive enumeration
    - Change discovery via delta queries
    - File download

Capture is pull only. The watch daemon polls Graph's delta endpoint on a
schedule, so this module never receives anything: it exposes no inbound
endpoint for content, there is no webhook subscription or notification
receiver, no tunnel and no third-party relay. Every call here is an
outbound HTTPS request from the on-prem host to Graph.

Usage:
    client = GraphClient(tenant_id="...", client_id="...", client_secret="...")
    sites = await client.list_sites()
    items, next_token = await client.get_changes(site_id, drive_id)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

try:
    import msal
    MSAL_AVAILABLE = True
except ImportError:
    MSAL_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)

# Microsoft Graph API base
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# ---- Data Classes ----

@dataclass
class SiteInfo:
    """A SharePoint site discovered via Graph API."""
    site_id: str
    display_name: str
    web_url: str
    description: str = ""


@dataclass
class DriveInfo:
    """A document library (drive) within a SharePoint site."""
    drive_id: str
    name: str
    web_url: str
    site_id: str = ""


# ---- Graph API Client ----

class GraphClient:
    """
    Authenticated Microsoft Graph API client.

    Uses client credentials (app-only) flow via MSAL.
    Token is cached and automatically refreshed.
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        if not MSAL_AVAILABLE:
            raise ImportError("msal is required. Run: pip install msal")
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is required. Run: pip install httpx")

        self.tenant_id = tenant_id or os.environ.get("AZURE_TENANT_ID", "")
        self.client_id = client_id or os.environ.get("AZURE_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get(
            "AZURE_CLIENT_SECRET", ""
        )

        if not all([self.tenant_id, self.client_id, self.client_secret]):
            raise ValueError(
                "Azure AD credentials required. Set AZURE_TENANT_ID, "
                "AZURE_CLIENT_ID, AZURE_CLIENT_SECRET environment variables."
            )

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        scopes = ["https://graph.microsoft.com/.default"]

        self._app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=authority,
        )
        self._scopes = scopes
        self._token: dict = {}

    # ---- Auth ----

    async def _get_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        # Check cache first
        result = self._app.acquire_token_silent(
            scopes=self._scopes, account=None
        )
        if result and "access_token" in result:
            return result["access_token"]

        # Acquire new token
        result = await asyncio.to_thread(
            self._app.acquire_token_for_client,
            scopes=self._scopes,
        )

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "Unknown"))
            raise RuntimeError(f"Failed to acquire Graph API token: {error}")

        self._token = result
        return result["access_token"]

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Make an authenticated Graph API request."""
        return await self._request_url(
            method, f"{GRAPH_API_BASE}{path}", json_body=json_body, params=params
        )

    async def _request_url(
        self,
        method: str,
        url: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Make an authenticated request to a FULL URL (e.g. delta links)."""
        token = await self._get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
            resp.raise_for_status()
            # 202/204 responses carry no body (sendMail,
            # sendActivityNotification, ...); resp.json() would raise
            # JSONDecodeError and mask a successful call as a failure.
            if not resp.content:
                return {}
            return resp.json()

    # ---- Site Discovery ----

    async def list_sites(self, search: str = "*") -> list[SiteInfo]:
        """Discover SharePoint sites the app has access to."""
        data = await self._request(
            "GET", "/sites", params={"search": search}
        )

        sites = []
        for item in data.get("value", []):
            sites.append(SiteInfo(
                site_id=item["id"],
                display_name=item.get("displayName", item.get("name", "")),
                web_url=item.get("webUrl", ""),
                description=item.get("description", ""),
            ))
        return sites

    async def get_site(self, site_id: str) -> SiteInfo:
        """Get a specific SharePoint site."""
        data = await self._request("GET", f"/sites/{site_id}")
        return SiteInfo(
            site_id=data["id"],
            display_name=data.get("displayName", data.get("name", "")),
            web_url=data.get("webUrl", ""),
            description=data.get("description", ""),
        )

    # ---- Drive / Document Library ----

    async def list_drives(self, site_id: str) -> list[DriveInfo]:
        """List document libraries in a SharePoint site."""
        data = await self._request("GET", f"/sites/{site_id}/drives")

        drives = []
        for item in data.get("value", []):
            drives.append(DriveInfo(
                drive_id=item["id"],
                name=item.get("name", ""),
                web_url=item.get("webUrl", ""),
                site_id=site_id,
            ))
        return drives

    async def list_folder(
        self, site_id: str, drive_id: str, folder_path: str = "/"
    ) -> list[dict]:
        """List files in a drive folder."""
        encoded = folder_path.rstrip("/") + ":/children"
        if folder_path == "/":
            data = await self._request(
                "GET", f"/sites/{site_id}/drives/{drive_id}/root/children"
            )
        else:
            data = await self._request(
                "GET",
                f"/sites/{site_id}/drives/{drive_id}/root:{encoded}",
            )
        return data.get("value", [])

    async def download_file(
        self, site_id: str, drive_id: str, item_id: str
    ) -> bytes:
        """Download a file's content from SharePoint."""
        token = await self._get_token()
        url = (
            f"{GRAPH_API_BASE}/sites/{site_id}/drives/{drive_id}"
            f"/items/{item_id}/content"
        )

        headers = {"Authorization": f"Bearer {token}"}

        # Graph returns a 302 to a SharePoint download.aspx URL with a
        # temp auth token — must follow redirects or every download fails
        # with HTTPStatusError on the redirect (fixed 2026-08-07 live test).
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content

    # ---- Delta Queries (polling fallback) ----

    async def get_changes(
        self,
        site_id: str,
        drive_id: str,
        delta_token: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Get file changes via delta query. Returns (items, next_delta_token)."""
        if delta_token:
            # Delta links from Graph are FULL URLs — pass them straight
            # through; _request would prepend the base and double the
            # host (fixed 2026-08-07 live test: 404 on every resume).
            if delta_token.startswith("http"):
                data = await self._request_url("GET", delta_token)
            else:
                data = await self._request("GET", delta_token)
        else:
            data = await self._request(
                "GET",
                f"/sites/{site_id}/drives/{drive_id}/root/delta",
            )

        items = data.get("value", [])
        next_token = data.get("@odata.deltaLink")

        return items, next_token
