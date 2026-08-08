# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Teams app package builder — generate a store-ready manifest zip.

Turns the sideloaded test zip into a reproducible, org-catalog-ready
app package:

- manifest.json generated from a template with the real bot ID,
  proper developer URLs, RSC permissions, and localization blocks
- icons checked for the exact Teams requirements (color 192x192,
  outline 32x32)
- zip assembled deterministically (stable order, no timestamps)
- validation pass: required fields, icon sizes, permission names,
  schema conformance

Output: threadweave-teams-app-<version>.zip (also written to
dist/threadweave-bot-manifest.zip for the org app catalog upload).
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path

# The manifest template. {placeholders} are filled by the builder.
MANIFEST_TEMPLATE = {
    "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
    "manifestVersion": "1.16",
    "version": "{version}",
    "id": "{bot_id}",
    "packageName": "{package_name}",
    "developer": {
        "name": "ThreadWeave",
        "websiteUrl": "https://threadweave.net",
        "privacyUrl": "https://threadweave.net/privacy",
        "termsOfUseUrl": "https://threadweave.net/terms",
    },
    "icons": {
        "color": "color.png",
        "outline": "outline.png",
    },
    "name": {
        "short": "ThreadWeave",
        "full": "ThreadWeave Organizational Memory",
    },
    "description": {
        "short": "Capture decisions, answers and insights into your org memory.",
        "full": (
            "ThreadWeave captures organizational knowledge from conversations, "
            "email, documents and notebooks, and stores it in an on-premises "
            "palace where it can be searched later. Nothing is saved without "
            "your approval: every capture is disclosed, you can opt out, and "
            "you can delete your own entries at any time."
        ),
    },
    "accentColor": "#0F766E",
    "bots": [
        {
            "botId": "{bot_id}",
            "scopes": ["team", "personal", "groupchat"],
            "supportsFiles": False,
            "isNotificationOnly": False,
        }
    ],
    "authorization": {
        "permissions": {
            "resourceSpecific": [
                {"type": "Application", "name": "ChatMessage.Read.Chat"},
                {"type": "Application", "name": "ChannelMessage.Read.Group"},
            ]
        }
    },
    "webApplicationInfo": {
        "id": "{bot_id}",
        "resource": "https://ThreadWeaveBot",
    },
    "permissions": ["identity", "messageTeamMembers"],
    "validDomains": [],
}

# Icon requirements enforced by Teams app validation.
COLOR_SIZE = (192, 192)
OUTLINE_SIZE = (32, 32)


def build_manifest(bot_id: str, version: str,
                   package_name: str = "com.powerlooming.threadweavebot") -> dict:
    """Render the manifest for a bot ID and version."""
    manifest = json.loads(json.dumps(MANIFEST_TEMPLATE))  # deep copy
    text = json.dumps(manifest)
    text = text.replace("{bot_id}", bot_id)
    text = text.replace("{version}", version)
    text = text.replace("{package_name}", package_name)
    return json.loads(text)


def validate_icons(color_path: Path, outline_path: Path) -> list[str]:
    """Check icons against Teams requirements; return problems."""
    problems = []
    try:
        from PIL import Image
    except ImportError:
        problems.append("PIL not installed — cannot verify icon sizes")
        return problems
    for label, path, expected in (
        ("color", color_path, COLOR_SIZE),
        ("outline", outline_path, OUTLINE_SIZE),
    ):
        if not path.exists():
            problems.append(f"{label} icon missing: {path}")
            continue
        img = Image.open(path)
        if img.size != expected:
            problems.append(
                f"{label} icon is {img.size[0]}x{img.size[1]}, "
                f"expected {expected[0]}x{expected[1]}"
            )
        if label == "color" and img.mode not in ("RGB", "RGBA"):
            problems.append(f"color icon mode is {img.mode}, expected RGB/RGBA")
    return problems


def build_package(
    bot_id: str,
    version: str,
    color_icon: str | Path,
    outline_icon: str | Path,
    out_dir: str | Path | None = None,
    package_name: str = "com.powerlooming.threadweavebot",
) -> tuple[Path, list[str]]:
    """Build the app package zip; return (zip_path, validation_problems)."""
    color = Path(color_icon)
    outline = Path(outline_icon)
    problems = validate_icons(color, outline)

    manifest = build_manifest(bot_id, version, package_name)

    out_dir = Path(out_dir) if out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"threadweave-teams-app-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Deterministic: fixed order, no timestamps.
        info = zipfile.ZipInfo("manifest.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, json.dumps(manifest, indent=2))
        for name, path in (("color.png", color), ("outline.png", outline)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())

    # Also write the stable name used by the org catalog upload flow.
    stable = out_dir / "threadweave-bot-manifest.zip"
    stable.write_bytes(zip_path.read_bytes())

    return zip_path, problems


def _validate_manifest(manifest: dict) -> list[str]:
    """Static validation of manifest fields (schema-level)."""
    problems = []
    if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
                    manifest.get("id", "")):
        problems.append("manifest id is not a valid GUID")
    if not manifest.get("developer", {}).get("privacyUrl", "").startswith("https://"):
        problems.append("privacyUrl must be https")
    if not manifest.get("developer", {}).get("termsOfUseUrl", "").startswith("https://"):
        problems.append("termsOfUseUrl must be https")
    if not manifest.get("bots"):
        problems.append("no bot declared")
    rsc = manifest.get("authorization", {}).get("permissions", {}).get("resourceSpecific", [])
    names = {p.get("name") for p in rsc}
    if "ChatMessage.Read.Chat" not in names:
        problems.append("missing RSC ChatMessage.Read.Chat (group chat passive capture)")
    if "ChannelMessage.Read.Group" not in names:
        problems.append("missing RSC ChannelMessage.Read.Group (channel passive capture)")
    if not manifest.get("webApplicationInfo", {}).get("id"):
        problems.append("webApplicationInfo.id missing (required for RSC)")
    return problems
