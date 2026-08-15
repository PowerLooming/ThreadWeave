# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
RSC consent verification — the "no silent @mention-only mode" probe.

The Teams manifest declares RSC permissions (ChannelMessage.Read.Group,
ChatMessage.Read.Chat) that let the bot passively receive ALL messages
in teams/chats where it is installed. Declaring them is not enough: a
tenant admin must GRANT consent (Teams admin center or PowerShell
preapproval). Without consent the bot silently degrades to
@mention-only capture and nothing fails loudly.

This module tracks the teams the bot has observed (from activity
channelData) and checks each team's granted RSC permissions via
Graph GET /teams/{id}/permissionGrants, matching clientAppId against
the bot's app id. Verified against Microsoft docs 2026-08-15:
"Check the RSC permissions granted to a specific resource ... Each
entry in the list can be correlated to the Teams app by matching the
clientAppId in the permission grants list with the webApplicationInfo.Id
property in the app's manifest."

Reading the grant list requires TeamsAppInstallation.ReadForTeam.All
(application) on the AZURE_* app registration.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SEEN_FILE = "~/.threadweave/teams_seen.json"

# Team GUIDs are canonical GUIDs; channel ids look like
# 19:<hex>@thread.tacv2 and are NOT valid /teams/{id} path segments.
_TEAM_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# RSC permissions the bot depends on for passive capture, by scope.
EXPECTED_PERMISSIONS = {
    "channel": "ChannelMessage.Read.Group",
    "groupchat": "ChatMessage.Read.Chat",
}

CONSENT_HINT = (
    "RSC consent missing: grant it in Teams admin center -> Teams apps "
    "-> Manage apps -> ThreadWeave -> Permissions -> 'Review permissions "
    "and consent' (menu labels vary by portal version), or preapprove "
    "org-wide via PowerShell (see docs/m365-connectors.md)."
)

READ_PERMISSION_HINT = (
    "Cannot verify RSC consent: the Graph call was refused. Grant "
    "TeamsAppInstallation.ReadForTeam.All (application) on the "
    "AZURE_CLIENT_ID app registration, or verify manually in the Teams "
    "admin center."
)


class TeamSeenStore:
    """Persist the team ids the bot has observed (install events, messages)."""

    def __init__(self, path: str | None = None):
        self.path = os.path.expanduser(
            path
            or os.environ.get("THREADWEAVE_TEAMS_SEEN_FILE", DEFAULT_SEEN_FILE)
        )
        self._teams: set[str] = self._load()

    def _load(self) -> set[str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return {t for t in data if t}
        except FileNotFoundError:
            return set()
        except Exception as exc:
            logger.warning("Team seen store load failed: %s", exc)
        return set()

    def _save(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._teams), fh, indent=2)
        except Exception as exc:
            logger.warning("Team seen store save failed: %s", exc)

    def add(self, team_id: str) -> bool:
        """Record a team id. Returns True if it was previously unseen."""
        if not team_id:
            return False
        if team_id in self._teams:
            return False
        self._teams.add(team_id)
        self._save()
        return True

    def all(self) -> list[str]:
        return sorted(self._teams)


async def check_team_consent(graph, team_id: str, bot_app_id: str) -> dict:
    """Check one team's granted RSC permissions for our app.

    Returns:
        {"team_id": ..., "status": "granted" | "missing" | "error",
         "permissions": [...], "detail": str}
    """
    if not team_id or not bot_app_id:
        return {"team_id": team_id, "status": "error",
                "permissions": [], "detail": "missing team id or bot app id"}
    if not _TEAM_GUID_RE.match(team_id):
        # Channel ids (19:...@thread.tacv2) are not valid here; the
        # caller should pass the team's AAD group GUID (aadGroupId).
        return {
            "team_id": team_id, "status": "error", "permissions": [],
            "detail": (
                f"'{team_id[:24]}...' is not a team GUID (channel-scoped "
                "install). Verify RSC consent manually in the Teams admin "
                "center: Teams apps -> Manage apps -> ThreadWeave -> "
                "Permissions -> Review permissions and consent."
            ),
        }
    try:
        data = await graph._request(
            "GET", f"/teams/{team_id}/permissionGrants"
        )
    except Exception as exc:
        status = ""
        try:
            status = f" HTTP {exc.response.status_code}"
        except Exception:
            pass
        if "403" in status or "401" in status:
            return {
                "team_id": team_id, "status": "error", "permissions": [],
                "detail": READ_PERMISSION_HINT + f" (Graph said{status})",
            }
        return {
            "team_id": team_id, "status": "error", "permissions": [],
            "detail": f"permissionGrants lookup failed: {exc}",
        }

    grants = data.get("value", []) if isinstance(data, dict) else []
    ours = [
        g for g in grants
        if isinstance(g, dict) and g.get("clientAppId") == bot_app_id
    ]
    permissions = sorted({
        g.get("permission", "") for g in ours if g.get("permission")
    })
    if ours:
        return {
            "team_id": team_id, "status": "granted",
            "permissions": permissions, "detail": "",
        }
    return {
        "team_id": team_id, "status": "missing", "permissions": [],
        "detail": CONSENT_HINT,
    }
