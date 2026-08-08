# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the Teams app package builder."""

import json
import zipfile

from threadweave.connectors.teams.package import (
    build_manifest, build_package, validate_icons, _validate_manifest,
)

BOT_ID = "cb342c61-8ab1-4c7b-ac0c-0a7f191acf4b"


def test_build_manifest_fields():
    m = build_manifest(BOT_ID, "1.0.1")
    assert m["id"] == BOT_ID
    assert m["version"] == "1.0.1"
    assert m["packageName"] == "com.powerlooming.threadweavebot"
    assert m["developer"]["privacyUrl"] == "https://threadweave.net/privacy"
    assert m["bots"][0]["botId"] == BOT_ID
    rsc = m["authorization"]["permissions"]["resourceSpecific"]
    names = {p["name"] for p in rsc}
    assert names == {"ChatMessage.Read.Chat", "ChannelMessage.Read.Group"}
    assert m["webApplicationInfo"]["id"] == BOT_ID


def test_validate_manifest_ok():
    assert _validate_manifest(build_manifest(BOT_ID, "1.0.0")) == []


def test_validate_manifest_catches_bad_id():
    m = build_manifest("not-a-guid", "1.0.0")
    assert any("GUID" in p for p in _validate_manifest(m))


def test_validate_manifest_catches_missing_rsc():
    m = build_manifest(BOT_ID, "1.0.0")
    m["authorization"]["permissions"]["resourceSpecific"] = []
    assert any("ChatMessage.Read.Chat" in p for p in _validate_manifest(m))


def test_validate_icons(tmp_path):
    from PIL import Image
    color = tmp_path / "color.png"
    outline = tmp_path / "outline.png"
    Image.new("RGB", (192, 192), (15, 118, 110)).save(color)
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(outline)
    assert validate_icons(color, outline) == []

    Image.new("RGB", (100, 100), (0, 0, 0)).save(outline)
    problems = validate_icons(color, outline)
    assert any("outline" in p and "32x32" in p for p in problems)


def test_build_package_content(tmp_path):
    from PIL import Image
    color = tmp_path / "color.png"
    outline = tmp_path / "outline.png"
    Image.new("RGB", (192, 192), (15, 118, 110)).save(color)
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(outline)

    out = tmp_path / "dist"
    zip_path, problems = build_package(BOT_ID, "1.0.0", color, outline, out)
    assert problems == []

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert names == ["manifest.json", "color.png", "outline.png"]
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["version"] == "1.0.0"

    # stable alias written too
    assert (out / "threadweave-bot-manifest.zip").exists()


def test_build_package_deterministic(tmp_path):
    from PIL import Image
    color = tmp_path / "color.png"
    outline = tmp_path / "outline.png"
    Image.new("RGB", (192, 192), (15, 118, 110)).save(color)
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(outline)

    a, _ = build_package(BOT_ID, "1.0.0", color, outline, tmp_path / "a")
    b, _ = build_package(BOT_ID, "1.0.0", color, outline, tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
