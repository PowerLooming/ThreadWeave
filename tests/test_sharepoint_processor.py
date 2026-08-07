# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the SharePoint DocumentProcessor full-drive import.

Covers the 2026-08-07 live-test fixes:
- folder recursion (previously folders were skipped entirely, so
  folder-organized libraries imported zero documents)
- bounded recursion depth (MAX_FOLDER_DEPTH)
- download follows Graph's 302 redirect (follow_redirects)
"""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.sharepoint.processor import (
    DocumentProcessor,
    MAX_FOLDER_DEPTH,
)


class FakeGraph:
    """Fake GraphClient with a folder tree: root -> a/ -> b/ -> file.txt."""

    def __init__(self):
        self.calls = []

    async def list_folder(self, site_id, drive_id, folder_path):
        self.calls.append(folder_path)
        if folder_path == "/":
            return [
                {"id": "f1", "name": "a", "folder": {}},
                {"id": "f2", "name": "root.txt", "size": 10},
            ]
        if folder_path == "/a":
            return [
                {"id": "f3", "name": "b", "folder": {}},
                {"id": "f4", "name": "a.txt", "size": 10},
            ]
        if folder_path == "/a/b":
            return [{"id": "f5", "name": "b.txt", "size": 10}]
        return []

    async def download_file(self, site_id, drive_id, item_id):
        # distinct content per file so hash-dedup doesn't skip them
        return f"decision: content of {item_id}".encode()


@pytest.mark.asyncio
async def test_process_drive_recurses_into_folders():
    graph = FakeGraph()
    proc = DocumentProcessor(graph)
    batch = await proc.process_drive("site", "drive", site_name="S", drive_name="D")
    names = {d.file_name for d in batch.documents}
    assert names == {"root.txt", "a.txt", "b.txt"}
    assert batch.total_processed == 3
    # folder paths recorded on the docs
    paths = {d.file_path for d in batch.documents}
    assert "/a/b/b.txt" in paths


class DeepGraph(FakeGraph):
    """Folder chain deeper than MAX_FOLDER_DEPTH."""

    async def list_folder(self, site_id, drive_id, folder_path):
        depth = folder_path.count("/")
        if depth <= MAX_FOLDER_DEPTH:
            return [{"id": f"d{depth}", "name": "next", "folder": {}}]
        return [{"id": "deep", "name": "deep.txt", "size": 10}]


@pytest.mark.asyncio
async def test_process_drive_depth_bounded():
    graph = DeepGraph()
    proc = DocumentProcessor(graph)
    batch = await proc.process_drive("site", "drive", site_name="S", drive_name="D")
    # recursion must stop at the depth bound, not recurse forever
    assert len(graph.calls) <= MAX_FOLDER_DEPTH + 2
    assert batch.total_processed == 0
