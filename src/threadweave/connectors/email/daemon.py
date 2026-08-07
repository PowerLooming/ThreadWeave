# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Email Watch Daemon — continuous polling of Exchange Online mailboxes.

Privacy contract: email content flows ONE WAY, M365 -> on-prem ThreadWeave,
via authenticated Graph API pulls. No webhook, no tunnel, no third-party
relay, no content ever leaving the tenant towards an external service and
back in again. The daemon polls on a schedule (pull model), so the only
network path is outbound from the on-prem host to Graph.

Pattern mirrors the GWS watcher (`threadweave gws watch`): a bounded loop
with per-tick stats and graceful Ctrl+C shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300          # seconds between polls
DEFAULT_MAX_RESULTS = 20        # unread messages fetched per poll
DEDUP_CAPACITY = 1000           # remembered message IDs (bounded set)


class EmailWatchDaemon:
    """Poll an Exchange Online mailbox and mine knowledge from new mail.

    Args:
        watcher: MailWatcher instance (fetch_unread, fetch_thread, mark_as_read)
        processor: EmailProcessor instance (process_message, process_thread)
        mailbox: UPN or email address of the monitored mailbox
        interval: seconds between polls
        max_results: unread messages fetched per poll
        mark_read: mark processed messages as read (default False — the
            daemon must not alter the user's mailbox unless explicitly asked)
        use_threads: group messages by conversation and process full threads
            (the CC-me pattern); requires fetch_thread on the watcher
    """

    def __init__(
        self,
        watcher,
        processor,
        mailbox: str,
        interval: int = DEFAULT_INTERVAL,
        max_results: int = DEFAULT_MAX_RESULTS,
        mark_read: bool = False,
        use_threads: bool = True,
    ):
        self.watcher = watcher
        self.processor = processor
        self.mailbox = mailbox
        self.interval = max(10, int(interval))
        self.max_results = max(1, int(max_results))
        self.mark_read = mark_read
        self.use_threads = use_threads
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self.stats = {
            "polls": 0,
            "messages_fetched": 0,
            "threads_processed": 0,
            "knowledge_extracted": 0,
            "skipped": 0,
            "errors": 0,
        }

    # ---- Public API ----

    async def run(self) -> None:
        """Run the polling loop until interrupted."""
        print(
            f"Email watcher: {self.mailbox} "
            f"(interval={self.interval}s, threads={self.use_threads}, "
            f"mark_read={self.mark_read})"
        )
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                tick = datetime.now(timezone.utc).isoformat()[:19]
                try:
                    result = await self.run_once()
                    if result["submitted"] > 0 or result["processed"] > 0:
                        print(f"[{tick}] {self._summarize(result)}")
                except Exception as e:
                    self.stats["errors"] += 1
                    logger.error("Poll failed: %s", e)
                    print(f"[{tick}] error: {e}")
                await asyncio.sleep(self.interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nEmail watcher stopped.")
            print(f"Total: {self._summarize(self.stats)}")

    async def run_once(self) -> dict:
        """One poll cycle. Returns a stats dict (also merged into self.stats)."""
        self.stats["polls"] += 1
        result = {
            "fetched": 0, "processed": 0, "submitted": 0,
            "skipped": 0, "errors": 0,
        }

        messages = []
        try:
            messages = await self.watcher.fetch_unread(
                mailbox=self.mailbox, max_results=self.max_results
            )
        except Exception as e:
            self.stats["errors"] += 1
            result["errors"] += 1
            logger.error("fetch_unread failed: %s", e)
            return result
        result["fetched"] = len(messages)
        self.stats["messages_fetched"] += len(messages)

        if not messages:
            return result

        if self.use_threads:
            processed_ids = await self._process_threads(messages, result)
        else:
            processed_ids = await self._process_messages(messages, result)

        # Optionally mark processed messages as read so they leave the
        # next unread poll (only when explicitly enabled).
        if self.mark_read and processed_ids:
            await self.watcher.mark_as_read(self.mailbox, processed_ids)

        return result

    # ---- Internals ----

    async def _process_threads(self, messages, result: dict) -> list[str]:
        """Group by conversationId and process each thread once.

        Returns the message IDs that were processed (for mark-as-read).
        """
        processed_ids: list[str] = []
        by_conversation: dict = {}
        for m in messages:
            if self._is_duplicate(m):
                result["skipped"] += 1
                continue
            by_conversation.setdefault(m.conversation_id or m.message_id, []).append(m)

        for conv_id, msgs in by_conversation.items():
            try:
                thread = await self.watcher.fetch_thread(self.mailbox, conv_id)
                processed = await self.processor.process_thread(thread)
                processed_ids.extend(m.message_id for m in msgs)
                self._tally(processed, result)
            except Exception as e:
                result["errors"] += 1
                self.stats["errors"] += 1
                logger.error("Thread %s failed: %s", conv_id, e)
        return processed_ids

    async def _process_messages(self, messages, result: dict) -> list[str]:
        """Process each message individually (no thread grouping).

        Returns the message IDs that were processed (for mark-as-read).
        """
        processed_ids: list[str] = []
        for m in messages:
            if self._is_duplicate(m):
                result["skipped"] += 1
                continue
            try:
                processed = await self.processor.process_message(m)
                processed_ids.append(m.message_id)
                self._tally(processed, result)
            except Exception as e:
                result["errors"] += 1
                self.stats["errors"] += 1
                logger.error("Message %s failed: %s", m.message_id, e)
        return processed_ids

    def _tally(self, processed, result: dict) -> None:
        result["processed"] += 1
        self.stats["threads_processed"] += 1
        if getattr(processed, "should_save", False):
            result["submitted"] += 1
            self.stats["knowledge_extracted"] += 1
        else:
            result["skipped"] += 1
            self.stats["skipped"] += 1

    def _is_duplicate(self, message) -> bool:
        """True if this message was already processed in a previous poll."""
        mid = message.message_id
        if mid in self._seen:
            return True
        self._mark_seen(message)
        return False

    def _mark_seen(self, message) -> bool:
        """Remember a message ID (bounded). Returns True if newly added."""
        mid = message.message_id
        if mid in self._seen:
            return False
        self._seen[mid] = None
        while len(self._seen) > DEDUP_CAPACITY:
            self._seen.popitem(last=False)
        return True

    @staticmethod
    def _summarize(stats: dict) -> str:
        return (
            f"fetched={stats.get('fetched', stats.get('messages_fetched', 0))} "
            f"processed={stats.get('processed', stats.get('threads_processed', 0))} "
            f"submitted={stats.get('submitted', stats.get('knowledge_extracted', 0))} "
            f"skipped={stats.get('skipped', 0)} errors={stats.get('errors', 0)}"
        )
