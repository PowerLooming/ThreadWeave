# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
SharePoint Watch Daemon — continuous delta-polling of document libraries.

Privacy contract: document content flows ONE WAY, M365 -> on-prem
ThreadWeave, via authenticated Graph API pulls. No webhook, no tunnel,
no third-party relay — the daemon polls Graph's delta endpoint on a
schedule, so the only network path is outbound from the on-prem host.

Delta queries return ONLY changes since the last poll (new, edited,
deleted items), so steady-state polls are cheap. Delta tokens persist
to a state file, so restarts resume where the last poll stopped
instead of re-crawling the whole library.

Pattern mirrors `threadweave email watch` and `threadweave gws watch`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300          # seconds between polls
DEFAULT_STATE_FILE = "~/.threadweave/sharepoint_delta.json"


class SharePointWatchDaemon:
    """Poll SharePoint drives for changes and mine new knowledge.

    Args:
        graph: GraphClient (list_sites, list_drives, get_changes, download_file)
        processor: DocumentProcessor (extract, detect, ingest helpers)
        interval: seconds between polls
        site_filter: optional display-name substring; only sites whose
            name contains it are watched
        state_file: JSON file persisting per-drive delta tokens
    """

    def __init__(
        self,
        graph,
        processor,
        interval: int = DEFAULT_INTERVAL,
        site_filter: str = "",
        state_file: str = DEFAULT_STATE_FILE,
        onenote_client=None,
    ):
        self.graph = graph
        self.processor = processor
        self.interval = max(10, int(interval))
        self.site_filter = (site_filter or "").strip().lower()
        self.state_file = os.path.expanduser(state_file)
        self.onenote = onenote_client  # optional OneNoteClient (delegated auth)
        self._state: dict[str, str] = {}
        self._load_state()
        self.stats = {
            "polls": 0,
            "sites": 0,
            "drives": 0,
            "changes_seen": 0,
            "documents_processed": 0,
            "knowledge_submitted": 0,
            "skipped": 0,
            "errors": 0,
            "onenote_pages_seen": 0,
            "onenote_processed": 0,
            "onenote_submitted": 0,
        }

    # ---- Public API ----

    async def run(self) -> None:
        """Run the polling loop until interrupted."""
        print(
            f"SharePoint watcher (interval={self.interval}s, "
            f"site_filter={self.site_filter or '*'}, "
            f"state={self.state_file})"
        )
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                tick = datetime.now(timezone.utc).isoformat()[:19]
                try:
                    result = await self.run_once()
                    if result["documents_processed"] > 0 or result["errors"] > 0:
                        print(f"[{tick}] {self._summarize(result)}")
                except Exception as e:
                    self.stats["errors"] += 1
                    logger.error("Poll failed: %s", e)
                    print(f"[{tick}] error: {e}")
                await asyncio.sleep(self.interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nSharePoint watcher stopped.")
            print(f"Total: {self._summarize(self.stats)}")

    async def run_once(self) -> dict:
        """One poll cycle over all sites/drives. Returns a stats dict."""
        self.stats["polls"] += 1
        result = {
            "sites": 0, "drives": 0, "changes": 0,
            "documents_processed": 0, "knowledge_submitted": 0,
            "skipped": 0, "errors": 0,
            "onenote_pages_seen": 0, "onenote_processed": 0,
            "onenote_submitted": 0,
        }

        try:
            sites = await self.graph.list_sites()
        except Exception as e:
            self.stats["errors"] += 1
            result["errors"] += 1
            logger.error("list_sites failed: %s", e)
            return result

        for site in sites:
            if self.site_filter and self.site_filter not in site.display_name.lower():
                continue
            result["sites"] += 1
            self.stats["sites"] += 1
            try:
                drives = await self.graph.list_drives(site.site_id)
            except Exception as e:
                result["errors"] += 1
                self.stats["errors"] += 1
                logger.error("list_drives %s failed: %s", site.display_name, e)
                continue
            for drive in drives:
                result["drives"] += 1
                self.stats["drives"] += 1
                await self._poll_drive(site, drive, result)

            if self.onenote is not None:
                await self._poll_onenote(site, result)

        self._save_state()
        return result

    # ---- Internals ----

    async def _poll_drive(self, site, drive, result: dict) -> None:
        """Delta-poll one drive and process changed items."""
        key = f"{site.site_id}/{drive.drive_id}"
        token = self._state.get(key)
        try:
            items, next_token = await self.graph.get_changes(
                site.site_id, drive.drive_id, delta_token=token
            )
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("get_changes %s failed: %s", key, e)
            return

        for item in items:
            result["changes"] += 1
            self.stats["changes_seen"] += 1
            await self._process_item(site, drive, item, result)

        if next_token:
            self._state[key] = next_token
        elif not token:
            # First poll without a delta link: nothing persisted yet.
            # Keep the key absent so the next poll re-crawls? No — a
            # missing deltaLink means no further changes were returned;
            # persist nothing and retry full delta next poll.
            pass

    async def _process_item(self, site, drive, item: dict, result: dict) -> None:
        """Download, extract, detect, and ingest one changed file."""
        # Deleted items carry a "deleted" key — nothing to mine.
        if "deleted" in item:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return
        # Folders are containers, not documents.
        if "folder" in item:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        # Opt-out gate: skip files authored by someone who declined
        # harvesting (don't even download their content).
        from threadweave.optout import OptOutStore

        author = (
            (item.get("createdBy") or {}).get("user") or {}
        ).get("email", "")
        if author:
            optout = OptOutStore()
            if optout.is_opted_out(author):
                result["skipped"] += 1
                self.stats["skipped"] += 1
                logger.info("Skipped %s (author %s opted out)", item.get("name"), author)
                return

        file_name = item.get("name", "")
        ext = Path(file_name).suffix.lower()
        if ext not in self.processor.SUPPORTED_EXTENSIONS:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        try:
            content = await self.graph.download_file(
                site.site_id, drive.drive_id, item["id"]
            )
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("download %s failed: %s", file_name, e)
            return
        if not content:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            logger.info("Empty content (placeholder?) skipped: %s", file_name)
            return

        text = self.processor._extract_text(content, ext, file_name)
        if not text.strip():
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        try:
            drawer_ids = await self.processor._mine_to_mempalace(
                text=text,
                wing=self.processor._sanitize_wing(site.display_name or site.site_id),
                room=self.processor._sanitize_room(drive.name or drive.drive_id),
                source_file=file_name,
                author_id=author,
            )
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("ingest %s failed: %s", file_name, e)
            return

        result["documents_processed"] += 1
        self.stats["documents_processed"] += 1
        if drawer_ids:
            result["knowledge_submitted"] += 1
            self.stats["knowledge_submitted"] += 1
            logger.info("Mined %s -> %s", file_name, drawer_ids)

    # ---- OneNote polling (watermark-based; no delta endpoint exists) ----

    async def _poll_onenote(self, site, result: dict) -> None:
        """Poll a site's OneNote notebooks for new/edited pages.

        OneNote has no delta API, so we track a per-site watermark: the
        latest lastModifiedDateTime seen. Each poll lists pages ordered
        by modified time (desc) and processes any page newer than the
        watermark — catching both NEW pages and EDITS (OneNote updates
        lastModifiedDateTime on edit).
        """
        key = f"onenote:{site.site_id}"
        watermark = self._state.get(key, "")
        try:
            pages = await self.onenote.get_recent_pages_with_text(site.site_id)
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("OneNote poll %s failed: %s", site.display_name, e)
            return

        newest = watermark
        for page in pages:
            modified = page.last_modified or ""
            if modified > newest:
                newest = modified
            result["changes"] += 1
            result["onenote_pages_seen"] += 1
            self.stats["onenote_pages_seen"] += 1
            # Skip pages we've already captured (watermark = inclusive)
            if watermark and modified <= watermark:
                result["skipped"] += 1
                self.stats["skipped"] += 1
                continue
            await self._process_onenote_page(site, page, result)

        if newest:
            self._state[key] = newest

    async def _process_onenote_page(self, site, page, result: dict) -> None:
        """Extract, detect, and ingest one OneNote page."""
        text = (page.text or "").strip()
        if len(text) < 50:  # empty/near-empty pages carry no knowledge
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        room = (page.section_name or "onenote").strip().lower().replace(" ", "_")
        try:
            drawer_ids = await self.processor._mine_to_mempalace(
                text=text,
                wing=self.processor._sanitize_wing(site.display_name or site.site_id),
                room=self.processor._sanitize_room(room),
                source_file=f"onenote:{page.title}",
            )
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("OneNote ingest %s failed: %s", page.title, e)
            return

        result["documents_processed"] += 1
        result["onenote_processed"] += 1
        self.stats["onenote_processed"] += 1
        if drawer_ids:
            result["knowledge_submitted"] += 1
            result["onenote_submitted"] += 1
            self.stats["onenote_submitted"] += 1
            logger.info("Mined OneNote page %s -> %s", page.title, drawer_ids)

    # ---- State persistence ----

    def _load_state(self) -> None:
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._state = {k: v for k, v in data.items() if v}
        except Exception as e:
            logger.warning("Failed to load delta state %s: %s", self.state_file, e)

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2)
        except Exception as e:
            logger.warning("Failed to save delta state %s: %s", self.state_file, e)

    @staticmethod
    def _summarize(stats: dict) -> str:
        return (
            f"sites={stats.get('sites', 0)} drives={stats.get('drives', 0)} "
            f"changes={stats.get('changes', stats.get('changes_seen', 0))} "
            f"processed={stats.get('documents_processed', 0)} "
            f"submitted={stats.get('knowledge_submitted', 0)} "
            f"onenote={stats.get('onenote_processed', 0)}/"
            f"{stats.get('onenote_submitted', 0)} "
            f"skipped={stats.get('skipped', 0)} errors={stats.get('errors', 0)}"
        )
