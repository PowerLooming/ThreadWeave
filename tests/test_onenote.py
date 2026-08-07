# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for OneNote support (HTML extraction + daemon watermark polling)."""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.sharepoint.onenote import OneNoteClient, html_to_text
from threadweave.connectors.sharepoint.daemon import SharePointWatchDaemon


# ---- HTML -> text ----

def test_html_to_text_strips_tags_and_blocks():
    html = (
        "<html><body><h1>Decisions</h1><p>We decided to use Redis "
        "for the session cache.</p><ul><li>Option A</li>"
        "<li>Option B</li></ul><script>var x=1;</script></body></html>"
    )
    text = html_to_text(html)
    assert "We decided to use Redis" in text
    assert "Option A" in text
    assert "var x" not in text  # script content stripped
    assert "<h1>" not in text


def test_html_to_text_empty_input():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


# ---- Daemon OneNote polling ----

class FakeOnenote:
    """Scripted pages: first poll 2 pages, second poll an edited/new page."""

    def __init__(self):
        self.calls = 0

    async def get_recent_pages_with_text(self, site_id):
        self.calls += 1
        if self.calls == 1:
            return [
                SimpleNamespace(page_id="p1", title="Decisions",
                                last_modified="2026-08-07T10:00:00Z",
                                section_name="General",
                                text="We decided to use Redis for the session cache to cut login latency"),
                SimpleNamespace(page_id="p2", title="Old",
                                last_modified="2026-08-01T10:00:00Z",
                                section_name="General",
                                text="Old notes about the onboarding checklist for new employees"),
            ]
        # second poll: p2 edited -> newer timestamp
        return [
            SimpleNamespace(page_id="p2", title="Old",
                            last_modified="2026-08-07T12:00:00Z",
                            section_name="General",
                            text="Old notes about onboarding. We decided to use PostgreSQL for the new service"),
        ]


class FakeProcessor:
    SUPPORTED_EXTENSIONS = {".txt"}

    def __init__(self):
        self.mined = []

    def _extract_text(self, content, ext, file_name):
        return content.decode()

    def _sanitize_wing(self, name):
        return name.lower().replace(" ", "_")

    def _sanitize_room(self, name):
        return name.lower()

    async def _mine_to_mempalace(self, text, wing, room, source_file):
        self.mined.append((source_file, wing, room))
        return [f"drawer-{source_file}"]


class FakeGraph:
    def __init__(self):
        self.site = SimpleNamespace(site_id="site1", display_name="Mark 8 Project Team")
        self.drive = SimpleNamespace(drive_id="drive1", name="Documents")

    async def list_sites(self):
        return [self.site]

    async def list_drives(self, site_id):
        return [self.drive]

    async def get_changes(self, site_id, drive_id, delta_token=None):
        return [], "https://graph.microsoft.com/v1.0/delta?token=z"

    async def download_file(self, site_id, drive_id, item_id):
        return b"x"


@pytest.mark.asyncio
async def test_first_onenote_poll_processes_all_pages_and_sets_watermark(tmp_path):
    onenote = FakeOnenote()
    d = SharePointWatchDaemon(FakeGraph(), FakeProcessor(),
                              onenote_client=onenote,
                              state_file=str(tmp_path / "s.json"))
    res = await d.run_once()

    assert res["onenote_pages_seen"] == 2
    assert res["documents_processed"] == 2  # both pages mined
    assert len(d.processor.mined) == 2
    # watermark persisted = newest modified
    assert d._state["onenote:site1"] == "2026-08-07T10:00:00Z"


@pytest.mark.asyncio
async def test_second_onenote_poll_only_processes_edited_page(tmp_path):
    onenote = FakeOnenote()
    d = SharePointWatchDaemon(FakeGraph(), FakeProcessor(),
                              onenote_client=onenote,
                              state_file=str(tmp_path / "s.json"))
    await d.run_once()
    res = await d.run_once()

    # p2 was edited (newer timestamp) -> processed; p1 already seen -> skipped
    assert res["onenote_pages_seen"] == 1
    assert res["documents_processed"] == 1
    assert len(d.processor.mined) == 3  # 2 from poll 1 + 1 edit from poll 2
    assert d._state["onenote:site1"] == "2026-08-07T12:00:00Z"


@pytest.mark.asyncio
async def test_onenote_disabled_by_default(tmp_path):
    d = SharePointWatchDaemon(FakeGraph(), FakeProcessor(),
                              state_file=str(tmp_path / "s.json"))
    assert d.onenote is None
    res = await d.run_once()
    assert res["onenote_pages_seen"] == 0


@pytest.mark.asyncio
async def test_short_onenote_pages_skipped(tmp_path):
    onenote = FakeOnenote()
    onenote.get_recent_pages_with_text = _short_pages  # type: ignore[assignment]

    d = SharePointWatchDaemon(FakeGraph(), FakeProcessor(),
                              onenote_client=onenote,
                              state_file=str(tmp_path / "s.json"))
    res = await d.run_once()
    assert res["documents_processed"] == 0
    assert res["skipped"] == 1


async def _short_pages(site_id):
    return [
        SimpleNamespace(page_id="p1", title="Tiny",
                        last_modified="2026-08-07T10:00:00Z",
                        section_name="General", text="hi"),
    ]
