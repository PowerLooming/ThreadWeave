# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the SharePoint watch daemon (delta polling)."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.sharepoint.daemon import SharePointWatchDaemon


class FakeGraph:
    """Fake GraphClient: one site, one drive, scripted delta responses."""

    def __init__(self):
        self.site = SimpleNamespace(
            site_id="site1", display_name="Mark 8 Project Team"
        )
        self.drive = SimpleNamespace(drive_id="drive1", name="Documents")
        self.polls = 0
        self.downloads = []
        self.tokens_seen = []

    async def list_sites(self):
        return [self.site]

    async def list_drives(self, site_id):
        return [self.drive]

    async def get_changes(self, site_id, drive_id, delta_token=None):
        self.tokens_seen.append(delta_token)
        self.polls += 1
        if self.polls == 1:
            # first poll: full listing + a delta link (FULL URL, as Graph returns)
            return [
                {"id": "f1", "name": "decisions.txt", "size": 10},
                {"id": "f2", "name": "notes.txt", "size": 10},
                {"id": "f3", "name": "old.txt", "size": 10, "deleted": {}},
                {"id": "f4", "name": "subfolder", "size": 0, "folder": {}},
            ], "https://graph.microsoft.com/v1.0/sites/site1/drive/drive1/root/delta?token=abc"
        # second poll: only the new file
        return [{"id": "f5", "name": "new.txt", "size": 10}], "https://graph.microsoft.com/v1.0/delta?token=def"

    async def download_file(self, site_id, drive_id, item_id):
        self.downloads.append(item_id)
        return f"we decided to use item {item_id}".encode()


class FakeProcessor:
    SUPPORTED_EXTENSIONS = {".txt", ".md"}

    def __init__(self):
        self.mined = []

    def _extract_text(self, content, ext, file_name):
        return content.decode()

    def _sanitize_wing(self, name):
        return name.lower().replace(" ", "_")

    def _sanitize_room(self, name):
        return name.lower()

    async def _mine_to_mempalace(self, text, wing, room, source_file, author_id=""):
        self.mined.append((source_file, wing, room))
        return [f"drawer-{source_file}"]


@pytest.mark.asyncio
async def test_first_poll_full_crawl_and_delta_token_persisted(tmp_path):
    graph = FakeGraph()
    proc = FakeProcessor()
    state = str(tmp_path / "state.json")
    d = SharePointWatchDaemon(graph, proc, state_file=state)

    res = await d.run_once()

    # 4 changes seen; deleted + folder skipped; 2 files processed
    assert res["changes"] == 4
    assert res["documents_processed"] == 2
    assert res["knowledge_submitted"] == 2
    assert res["skipped"] == 2
    # wing/room from site/drive names
    assert ("decisions.txt", "mark_8_project_team", "documents") in proc.mined
    # token persisted
    state = Path(state)
    assert state.exists()
    import json
    assert json.loads(state.read_text())["site1/drive1"] == "https://graph.microsoft.com/v1.0/sites/site1/drive/drive1/root/delta?token=abc"


@pytest.mark.asyncio
async def test_second_poll_uses_delta_token_and_only_new_changes(tmp_path):
    graph = FakeGraph()
    proc = FakeProcessor()
    d = SharePointWatchDaemon(graph, proc, state_file=str(tmp_path / "s.json"))

    await d.run_once()
    res = await d.run_once()

    # second poll used the persisted token
    assert graph.tokens_seen[1] == "https://graph.microsoft.com/v1.0/sites/site1/drive/drive1/root/delta?token=abc"
    assert res["changes"] == 1
    assert res["documents_processed"] == 1
    assert graph.downloads == ["f1", "f2", "f5"]


@pytest.mark.asyncio
async def test_state_resumes_after_restart(tmp_path):
    graph = FakeGraph()
    proc = FakeProcessor()
    state = str(tmp_path / "s.json")

    d1 = SharePointWatchDaemon(graph, proc, state_file=state)
    await d1.run_once()

    # "restart": new daemon instance reads the same state file
    d2 = SharePointWatchDaemon(graph, proc, state_file=state)
    res = await d2.run_once()
    # instance 1 poll 1: no token; instance 2 poll 1: RESUMED from file
    assert graph.tokens_seen[0] is None
    assert graph.tokens_seen[1] == "https://graph.microsoft.com/v1.0/sites/site1/drive/drive1/root/delta?token=abc"
    assert res["changes"] == 1  # only the new file, no full re-crawl
    assert res["documents_processed"] == 1


@pytest.mark.asyncio
async def test_site_filter_skips_other_sites(tmp_path):
    class TwoSiteGraph(FakeGraph):
        async def list_sites(self):
            return [
                self.site,
                SimpleNamespace(site_id="site2", display_name="Sales and Marketing"),
            ]

        async def list_drives(self, site_id):
            if site_id == "site2":
                return []  # would be polled without the filter
            return [self.drive]

    graph = TwoSiteGraph()
    d = SharePointWatchDaemon(graph, FakeProcessor(),
                              site_filter="mark 8", state_file=str(tmp_path / "s.json"))
    res = await d.run_once()
    assert res["sites"] == 1  # only Mark 8 matched
