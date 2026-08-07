# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
SharePoint Watcher — Microsoft Graph API client + webhook subscriptions.

Handles:
    - Azure AD authentication (client credentials)
    - Webhook subscription lifecycle (create, renew, delete)
    - Document discovery via delta queries
    - Site and drive enumeration

Usage:
    watcher = WebhookManager(tenant_id="...", client_id="...", client_secret="...")
    await watcher.create_subscription(
        site_id="...",
        list_id="documents",
        notification_url="https://threadweave.example.com/api/v1/webhooks/graph",
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

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

# Webhook max lifetime (Microsoft enforces 3 days max for most resources)
MAX_SUBSCRIPTION_DAYS = 3

# Renew when < 1 day remaining
RENEW_THRESHOLD_HOURS = 24


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


@dataclass
class Subscription:
    """A Microsoft Graph change notification subscription."""
    subscription_id: str
    resource: str  # e.g. /sites/{id}/lists/{id}
    notification_url: str
    expiration: datetime
    client_state: str = ""  # Secret for validation


@dataclass
class ChangeNotification:
    """A received change notification from Microsoft Graph."""
    subscription_id: str
    resource: str
    change_type: str  # created, updated, deleted
    site_id: str
    list_id: str
    item_id: str
    client_state: str = ""


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


# ---- Webhook Manager ----

class WebhookManager:
    """
    Manages Microsoft Graph change notification subscriptions.

    Subscriptions have a maximum lifetime enforced by Microsoft
    (currently 3 days for most resources). This manager handles
    creation, renewal, and cleanup.

    ThreadWeave webhook flow:
        1. Register subscription for a SharePoint list
        2. Microsoft sends POST to our notification_url on changes
        3. ThreadWeave validates the notification (client_state)
        4. ThreadWeave fetches changed items and mines them
        5. Subscription is renewed before expiration
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        self.graph = GraphClient(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        self._subscriptions: dict[str, Subscription] = {}

    # ---- Subscription Lifecycle ----

    async def create_subscription(
        self,
        site_id: str,
        list_id: str = "documents",
        notification_url: str = "",
        client_state: str = "",
    ) -> Subscription:
        """
        Create a change notification subscription for a SharePoint list.

        Args:
            site_id: SharePoint site ID (GUID format: tenant.sharepoint.com,guid)
            list_id: List/library ID or well-known name (e.g. 'documents')
            notification_url: HTTPS endpoint that receives notifications
            client_state: Secret for validating incoming notifications
        """
        resource = f"/sites/{site_id}/lists/{list_id}"

        expiration = datetime.now(timezone.utc) + timedelta(
            days=MAX_SUBSCRIPTION_DAYS
        )

        body = {
            "changeType": "created,updated",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration.isoformat(),
            "clientState": client_state,
        }

        try:
            data = await self.graph._request(
                "POST", "/subscriptions", json_body=body
            )
        except Exception as e:
            logger.error("Failed to create subscription for %s: %s", resource, e)
            raise

        sub = Subscription(
            subscription_id=data["id"],
            resource=data["resource"],
            notification_url=data["notificationUrl"],
            expiration=datetime.fromisoformat(
                data["expirationDateTime"].replace("Z", "+00:00")
            ),
            client_state=client_state,
        )

        self._subscriptions[sub.subscription_id] = sub
        logger.info(
            "Subscription created: %s -> %s (expires %s)",
            sub.subscription_id, resource, sub.expiration.isoformat()
        )
        return sub

    async def renew_subscription(self, subscription_id: str) -> Subscription:
        """Extend an existing subscription's expiration."""
        expiration = datetime.now(timezone.utc) + timedelta(
            days=MAX_SUBSCRIPTION_DAYS
        )

        body = {"expirationDateTime": expiration.isoformat()}

        data = await self.graph._request(
            "PATCH", f"/subscriptions/{subscription_id}", json_body=body
        )

        sub = self._subscriptions.get(subscription_id)
        if sub:
            sub.expiration = datetime.fromisoformat(
                data["expirationDateTime"].replace("Z", "+00:00")
            )

        logger.info("Subscription %s renewed until %s", subscription_id, expiration)
        return sub

    async def delete_subscription(self, subscription_id: str):
        """Delete a subscription."""
        await self.graph._request("DELETE", f"/subscriptions/{subscription_id}")
        self._subscriptions.pop(subscription_id, None)
        logger.info("Subscription %s deleted", subscription_id)

    async def list_subscriptions(self) -> list[Subscription]:
        """List all active subscriptions."""
        data = await self.graph._request("GET", "/subscriptions")

        subs = []
        for item in data.get("value", []):
            subs.append(Subscription(
                subscription_id=item["id"],
                resource=item["resource"],
                notification_url=item["notificationUrl"],
                expiration=datetime.fromisoformat(
                    item["expirationDateTime"].replace("Z", "+00:00")
                ),
                client_state=item.get("clientState", ""),
            ))
        return subs

    async def renew_expiring(self) -> int:
        """Renew all subscriptions expiring soon. Returns count renewed."""
        count = 0
        threshold = datetime.now(timezone.utc) + timedelta(
            hours=RENEW_THRESHOLD_HOURS
        )

        for sub in await self.list_subscriptions():
            if sub.expiration < threshold:
                try:
                    await self.renew_subscription(sub.subscription_id)
                    count += 1
                except Exception as e:
                    logger.error(
                        "Failed to renew %s: %s", sub.subscription_id, e
                    )

        return count

    # ---- Notification Handling ----

    @staticmethod
    def parse_notifications(payload: dict) -> list[ChangeNotification]:
        """
        Parse Microsoft Graph notification payload.

        Validates client_state if provided.

        Example payload:
        {
            "value": [{
                "subscriptionId": "...",
                "resource": "sites/{id}/lists/{id}",
                "changeType": "created",
                "clientState": "secret",
                "resourceData": {
                    "id": "item-guid",
                    "@odata.type": "#Microsoft.Graph.driveItem"
                }
            }]
        }
        """
        notifications = []
        for item in payload.get("value", []):
            resource_data = item.get("resourceData", {})
            resource = item.get("resource", "")

            # Extract site_id and list_id from resource path
            parts = resource.strip("/").split("/")
            site_id = parts[1] if len(parts) > 1 else ""
            list_id = parts[3] if len(parts) > 3 else ""

            notifications.append(ChangeNotification(
                subscription_id=item.get("subscriptionId", ""),
                resource=resource,
                change_type=item.get("changeType", ""),
                site_id=site_id,
                list_id=list_id,
                item_id=resource_data.get("id", ""),
                client_state=item.get("clientState", ""),
            ))

        return notifications

    def validate_notification(
        self, notification: ChangeNotification, expected_state: str = ""
    ) -> bool:
        """Validate a notification's client_state matches."""
        if expected_state and notification.client_state != expected_state:
            logger.warning("Invalid client_state in notification")
            return False
        return True


# ---- Auto-discovery ----

async def discover_and_watch(
    watcher: WebhookManager,
    notification_url: str,
    client_state: str = "",
    drive_filter: list[str] | None = None,
) -> list[Subscription]:
    """
    Discover all accessible SharePoint document libraries and subscribe.

    Args:
        watcher: Configured WebhookManager
        notification_url: HTTPS webhook endpoint
        client_state: Validation secret
        drive_filter: Optional list of library names to watch (all if None)

    Returns:
        List of created subscriptions
    """
    subscriptions = []

    sites = await watcher.graph.list_sites()
    logger.info("Discovered %d SharePoint sites", len(sites))

    for site in sites:
        logger.info("Scanning site: %s", site.display_name)
        try:
            drives = await watcher.graph.list_drives(site.site_id)
        except Exception as e:
            logger.warning("Failed to list drives for %s: %s", site.display_name, e)
            continue

        for drive in drives:
            if drive_filter and drive.name not in drive_filter:
                continue

            logger.info("  Found library: %s", drive.name)
            try:
                sub = await watcher.create_subscription(
                    site_id=site.site_id,
                    list_id=drive.drive_id,
                    notification_url=notification_url,
                    client_state=client_state,
                )
                subscriptions.append(sub)
            except Exception as e:
                logger.warning(
                    "Failed to subscribe to %s/%s: %s",
                    site.display_name, drive.name, e,
                )

    logger.info("Created %d subscriptions", len(subscriptions))
    return subscriptions
