# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Delegated mail access — the owner's own credentials, not an application's.

The managed tier reads a mailbox with app-only client credentials, which is
right for an org-wide capture connector. The single-user tier must not: its
promise is that the owner's own consent and the owner's own access bring the
data in, with nothing an admin had to grant and nothing the owner could not
already read in Outlook themselves.

This mirrors the OneNote delegated flow (msal public client, device-code
sign-in, shared token cache) so one sign-in on a machine covers every
delegated connector, and it keeps `ingest_graph_mail.py`'s client id and
scope so existing standalone setups keep working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import msal

logger = logging.getLogger(__name__)

# Microsoft's public Azure CLI client id, the same one the standalone mail
# harvester, the OneNote and the Teams publishing flows already default to:
# it is Microsoft's own registration, carries no tenant identity, and is
# overridable for tenants that registered their own device-code app.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
MAIL_SCOPES = ["Mail.Read"]
DEFAULT_CACHE = "~/.threadweave/msal_cache.json"


class DelegatedMailAuth:
    """Device-code sign-in for the mailbox owner's own Graph access."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        cache_file: Optional[str] = None,
    ):
        # THREADWEAVE_MAIL_TENANT_ID is what the standalone harvester uses;
        # AZURE_TENANT_ID is what the app-only connectors use. Accept both so
        # one .env serves either mode, and fall back to "common" (multi-tenant).
        self.tenant_id = (
            tenant_id
            or os.environ.get("THREADWEAVE_MAIL_TENANT_ID")
            or os.environ.get("AZURE_TENANT_ID", "")
        )
        self.client_id = (
            client_id
            or os.environ.get("THREADWEAVE_MAIL_CLIENT_ID")
            or DEFAULT_CLIENT_ID
        )
        self.cache_file = os.path.expanduser(
            cache_file
            or os.environ.get("THREADWEAVE_MSAL_CACHE")
            or DEFAULT_CACHE
        )
        self._cache = msal.SerializableTokenCache()
        if os.path.exists(self.cache_file):
            try:
                self._cache.deserialize(
                    Path(self.cache_file).read_text(encoding="utf-8")
                )
            except Exception as exc:  # a corrupt cache must not be fatal
                logger.warning("Failed to load MSAL cache: %s", exc)

        authority = (
            f"https://login.microsoftonline.com/{self.tenant_id}"
            if self.tenant_id
            else "https://login.microsoftonline.com/common"
        )
        self._app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=authority,
            token_cache=self._cache,
        )

    # ---- cache ----

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        try:
            Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.cache_file).write_text(self._cache.serialize(), encoding="utf-8")
            os.chmod(self.cache_file, 0o600)
        except Exception as exc:
            logger.warning("Failed to save MSAL cache: %s", exc)

    def account(self) -> str:
        """The signed-in account, or "" when nobody has signed in yet."""
        accounts = self._app.get_accounts()
        if not accounts:
            return ""
        return accounts[0].get("username", "") or ""

    # ---- tokens ----

    def _device_flow_hint(self, flow: dict) -> str:
        """Turn the two ways a device sign-in gets refused into instructions.

        A bare 'unauthorized_client' or an AADSTS65002 code is what an operator
        actually sees, and neither says what to change in Entra.
        """
        error = str(flow.get("error", ""))
        description = str(flow.get("error_description", ""))
        if "65002" in description or "65002" in error:
            return (
                f"Graph refused the sign-in for client {self.client_id} "
                "(AADSTS65002): Microsoft's own first-party clients are blocked "
                "for Mail.Read. Register an app of your own, give it the "
                "delegated Mail.Read permission, and set "
                "THREADWEAVE_MAIL_CLIENT_ID to its application (client) id."
            )
        if error == "unauthorized_client":
            return (
                f"Client {self.client_id} is not allowed to use the device "
                "sign-in flow. In Entra, open that app registration, then "
                "Authentication, and set 'Allow public client flows' to Yes; "
                "it also needs the delegated Mail.Read permission. Microsoft's "
                f"public Azure CLI client (the {DEFAULT_CLIENT_ID} default) is "
                "not usable for mail, so point THREADWEAVE_MAIL_CLIENT_ID at an "
                "app you registered."
            )
        return f"Device flow failed: {error or flow}"

    def get_token(self, interactive: bool = False) -> str:
        """Return a delegated Mail.Read token for the signed-in owner.

        Silent refresh first. Without a cached account this raises unless
        ``interactive`` is set, because a daemon must never block waiting for
        a sign-in that nobody is watching.
        """
        accounts = self._app.get_accounts()
        result = None
        if accounts:
            result = self._app.acquire_token_silent(MAIL_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._save_cache()
            return result["access_token"]

        if not interactive:
            raise RuntimeError(
                "No delegated mail token. Run 'threadweave email login' once "
                "as the mailbox owner to sign in (device code); the daemon "
                "then refreshes silently."
            )

        flow = self._app.initiate_device_flow(scopes=MAIL_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(self._device_flow_hint(flow))

        print("\nSign in to read your own mailbox with ThreadWeave:")
        print(f"  1. Open:  {flow['verification_uri']}")
        print(f"  2. Enter code:  {flow['user_code']}")
        print("  Waiting for sign-in...\n")

        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(
                f"Sign-in failed: {result.get('error_description', result)}"
            )
        self._save_cache()
        print("Signed in. Token cached — the daemon refreshes it silently.\n")
        return result["access_token"]
