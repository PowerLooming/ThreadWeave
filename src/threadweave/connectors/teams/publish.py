# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Teams app publishing — upload a package to an organization's app
catalog via the Graph API.

Tier 2 of the distribution story: instead of the admin clicking
through the Teams admin center, one command uploads the app package
to the org catalog. The admin consents AppCatalog.Submit once via
device-code sign-in (same pattern as OneNote), and the app appears
under Teams admin center → Teams apps → Manage apps, ready to
publish for users.

Graph endpoint: POST /appCatalogs/teamsApps with the zip as the body.
Requires AppCatalog.Submit (delegated) — the signed-in user must be
a Teams admin or app catalog submitter.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CACHE_FILE = "~/.threadweave/msal_cache.json"
# The same public client used for OneNote device flow. The user's
# identity + consent is the credential — no client secret on the host.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
PUBLISH_SCOPES = ["AppCatalog.Submit", "User.Read"]

# Teams app catalog app types: "teamsApp" for org catalog uploads.
APP_TYPES = ("teamsApp", "teamsApp@microsoft.graph.embed")


class TeamsAppPublisher:
    """Publishes Teams app packages to an org's app catalog."""

    def __init__(self, client_id: str | None = None,
                 cache_file: str = DEFAULT_CACHE_FILE,
                 api_base: str = GRAPH_API_BASE):
        import msal

        self.client_id = client_id or os.environ.get(
            "THREADWEAVE_PUBLISH_CLIENT_ID", DEFAULT_CLIENT_ID
        )
        self.api_base = api_base.rstrip("/")
        self._cache_file = os.path.expanduser(cache_file)
        self._app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority="https://login.microsoftonline.com/common",
            token_cache=msal.SerializableTokenCache(),
        )
        if os.path.exists(self._cache_file):
            try:
                self._app.token_cache.deserialize(
                    Path(self._cache_file).read_text(encoding="utf-8")
                )
            except Exception:
                logger.warning("Could not load MSAL cache %s",
                               self._cache_file)

    def _save_cache(self) -> None:
        if self._app.token_cache.has_state_changed:
            Path(self._cache_file).write_text(
                self._app.token_cache.serialize(), encoding="utf-8"
            )

    def get_token(self, interactive: bool = True) -> str:
        """Return an access token, refreshing or re-signing-in."""
        accounts = self._app.get_accounts()
        result = None
        if accounts:
            result = self._app.acquire_token_silent(
                PUBLISH_SCOPES, account=accounts[0]
            )
        if result and "access_token" in result:
            self._save_cache()
            return result["access_token"]

        if not interactive:
            raise RuntimeError(
                "No cached publish token. Run 'threadweave teams "
                "publish' interactively once to sign in."
            )

        flow = self._app.initiate_device_flow(scopes=PUBLISH_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error')}")

        print("\nSign in to publish the ThreadWeave app to your org:")
        print(f"  1. Open:  {flow['verification_uri']}")
        print(f"  2. Enter code:  {flow['user_code']}")
        print("  Waiting for sign-in...\n")

        result = self._app.acquire_token_by_device_flow(flow)
        self._save_cache()
        if "access_token" not in result:
            raise RuntimeError(
                f"Sign-in failed: {result.get('error_description', result)}"
            )
        return result["access_token"]

    def upload(self, zip_path: str | Path) -> dict:
        """Upload a package zip to the org app catalog.

        Returns the created teamsApp object (id, externalId, status).
        """
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
        }
        data = Path(zip_path).read_bytes()
        resp = requests.post(
            f"{self.api_base}/appCatalogs/teamsApps",
            headers=headers,
            data=data,
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Upload failed ({resp.status_code}): {resp.text[:400]}"
            )
        return resp.json()

    def wait_ready(self, app_id: str, timeout: int = 120) -> dict:
        """Poll until the app is ready in the catalog (uploads are
        processed asynchronously)."""
        token = self.get_token(interactive=False)
        headers = {"Authorization": f"Bearer {token}"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = requests.get(
                f"{self.api_base}/appCatalogs/teamsApps/{app_id}",
                headers=headers, timeout=60,
            )
            if resp.status_code == 200:
                return resp.json()
            time.sleep(5)
        raise TimeoutError(f"App {app_id} not ready after {timeout}s")

    def list_catalog_apps(self) -> list[dict]:
        """List org catalog apps (for verification)."""
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{self.api_base}/appCatalogs/teamsApps",
            headers=headers, timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"List failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json().get("value", [])
