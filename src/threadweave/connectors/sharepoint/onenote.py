# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
OneNote client for ThreadWeave — reads notebook pages via Microsoft Graph.

WHY NOT THE .one BINARY: OneNote's native format is a proprietary binary
that cannot be parsed reliably. Microsoft's supported path is the Graph
OneNote API, which returns pages as HTML.

WHY DELEGATED AUTH: Microsoft DEPRECATED app-only tokens for the OneNote
API on 2025-03-31 ("this API will no longer support app-only tokens...
Customers may still call these APIs using delegated (app+user) tokens").
So this client uses the device-code flow: the user signs in once in a
browser, MSAL persists the token cache, and the daemon silently refreshes
thereafter. Privacy contract unchanged: content flows ONE WAY M365 ->
on-prem via outbound pull; the delegated token only grants read access.

Scopes needed on the app registration (e.g. GraphReader):
    Notes.Read.All  (Application)  — unused now, but keeps option open
    Notes.Read      (Delegated)    — required for user-context reads
The delegated consent happens through the device-code sign-in itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CACHE_FILE = "~/.threadweave/msal_cache.json"

# Well-known public client for device code flows (Azure CLI). Using a
# public client avoids needing a client secret on the daemon host —
# the user's identity + consent is the credential.
# NOTE: configurable via THREADWEAVE_ONENOTE_CLIENT_ID; the default is
# the Azure CLI public client, which is broadly consented in tenants.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

# Site-level OneNote reads require Notes.Read.All (delegated) — the API
# rejects Notes.Read alone with 40004 listing the required scopes
# (verified live 2026-08-07 against /sites/{id}/onenote/pages).
ONENOTE_SCOPES = ["Notes.Read.All", "User.Read"]


class OneNotePage:
    """A OneNote page's metadata + extracted text."""

    def __init__(self, page_id: str, title: str, last_modified: str,
                 section_name: str = "", text: str = ""):
        self.page_id = page_id
        self.title = title
        self.last_modified = last_modified
        self.section_name = section_name
        self.text = text

    @property
    def modified_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(
                self.last_modified.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            return None


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text converter (blocks, list items, code)."""

    BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "tr", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    """Convert OneNote page HTML to clean text."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as e:
        logger.warning("HTML parse failed: %s", e)
        return ""
    return parser.text()


class OneNoteClient:
    """Delegated-auth OneNote reader using MSAL device-code + persisted cache."""

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        cache_file: str = DEFAULT_CACHE_FILE,
    ):
        import msal

        self.tenant_id = tenant_id or os.environ.get("AZURE_TENANT_ID", "")
        self.client_id = client_id or os.environ.get(
            "THREADWEAVE_ONENOTE_CLIENT_ID", DEFAULT_CLIENT_ID
        )
        self.cache_file = os.path.expanduser(cache_file)
        self._msal = msal
        self._cache = msal.SerializableTokenCache()
        if os.path.exists(self.cache_file):
            try:
                self._cache.deserialize(
                    Path(self.cache_file).read_text(encoding="utf-8")
                )
            except Exception as e:
                logger.warning("Failed to load MSAL cache: %s", e)

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

    # ---- Auth ----

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            try:
                Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
                Path(self.cache_file).write_text(
                    self._cache.serialize(), encoding="utf-8"
                )
            except Exception as e:
                logger.warning("Failed to save MSAL cache: %s", e)

    def get_token(self, interactive: bool = False) -> str:
        """Return an access token, refreshing or re-signing-in as needed."""
        accounts = self._app.get_accounts()
        result = None
        if accounts:
            result = self._app.acquire_token_silent(
                ONENOTE_SCOPES, account=accounts[0]
            )
        if result and "access_token" in result:
            self._save_cache()
            return result["access_token"]

        if not interactive:
            raise RuntimeError(
                "No cached OneNote token. Run 'threadweave sharepoint "
                "onenote-login' once to sign in."
            )

        flow = self._app.initiate_device_flow(scopes=ONENOTE_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error')}")

        print(f"\nSign in to Microsoft Graph for OneNote:")
        print(f"  1. Open:  {flow['verification_uri']}")
        print(f"  2. Enter code:  {flow['user_code']}")
        print("  Waiting for sign-in...\n")

        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(
                f"Sign-in failed: {result.get('error_description', result)}"
            )
        self._save_cache()
        print("Signed in. Token cached — daemon will refresh silently.\n")
        return result["access_token"]

    # ---- OneNote API ----

    async def list_pages(
        self,
        site_id: str,
        top: int = 50,
        interactive: bool = False,
    ) -> list[OneNotePage]:
        """List the most recently modified pages in a site's notebooks."""
        import httpx

        token = self.get_token(interactive=interactive)
        url = f"{GRAPH_API_BASE}/sites/{site_id}/onenote/pages"
        params = {
            "$top": top,
            "$orderby": "lastModifiedDateTime desc",
            "$select": "id,title,lastModifiedDateTime,parentSection",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params
            )
            resp.raise_for_status()
            data = resp.json()

        pages = []
        for item in data.get("value", []):
            section = item.get("parentSection", {}) or {}
            pages.append(OneNotePage(
                page_id=item.get("id", ""),
                title=item.get("title", "") or "(untitled)",
                last_modified=item.get("lastModifiedDateTime", ""),
                section_name=section.get("displayName", ""),
            ))
        return pages

    async def fetch_page_text(
        self,
        site_id: str,
        page_id: str,
        interactive: bool = False,
    ) -> str:
        """Fetch a page's HTML content and convert it to clean text."""
        import httpx

        token = self.get_token(interactive=interactive)
        url = f"{GRAPH_API_BASE}/sites/{site_id}/onenote/pages/{page_id}/content"
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            resp.raise_for_status()
            html = resp.text
        return html_to_text(html)

    async def get_recent_pages_with_text(
        self,
        site_id: str,
        top: int = 50,
        interactive: bool = False,
    ) -> list[OneNotePage]:
        """List recent pages AND fetch their text (used by the daemon)."""
        pages = await self.list_pages(site_id, top=top, interactive=interactive)
        for page in pages:
            try:
                page.text = await self.fetch_page_text(
                    site_id, page.page_id, interactive=interactive
                )
            except Exception as e:
                logger.warning("Fetch text failed for %s: %s", page.page_id, e)
        return pages
