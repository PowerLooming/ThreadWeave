# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the email watch daemon (continuous M365 -> on-prem polling)."""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.email.daemon import EmailWatchDaemon


def make_message(mid: str, conv: str = "c1", subject: str = "s"):
    return SimpleNamespace(
        message_id=mid, conversation_id=conv, subject=subject,
    )


class FakeWatcher:
    def __init__(self, messages):
        self.messages = list(messages)  # queue per poll
        self.marked = []
        self.thread_fetches = []

    async def fetch_unread(self, mailbox, max_results):
        out, self.messages = self.messages[:max_results], self.messages[max_results:]
        return out

    async def fetch_thread(self, mailbox, conversation_id):
        self.thread_fetches.append(conversation_id)
        return SimpleNamespace(conversation_id=conversation_id, subject="t")

    async def mark_as_read(self, mailbox, message_ids):
        self.marked.extend(message_ids)


class FakeProcessor:
    def __init__(self, should_save=True):
        self.should_save = should_save
        self.processed_threads = 0
        self.processed_messages = 0

    async def process_thread(self, thread):
        self.processed_threads += 1
        return SimpleNamespace(should_save=self.should_save)

    async def process_message(self, message):
        self.processed_messages += 1
        return SimpleNamespace(should_save=self.should_save)


@pytest.mark.asyncio
async def test_run_once_processes_threads_and_tallies():
    w = FakeWatcher([make_message("m1", "c1"), make_message("m2", "c1"),
                     make_message("m3", "c2")])
    p = FakeProcessor(should_save=True)
    d = EmailWatchDaemon(w, p, mailbox="kb@example.com", use_threads=True)

    res = await d.run_once()

    assert res["fetched"] == 3
    assert res["processed"] == 2          # two conversations -> two threads
    assert res["submitted"] == 2
    assert w.thread_fetches == ["c1", "c2"]
    assert p.processed_threads == 2


@pytest.mark.asyncio
async def test_run_once_no_threads_processes_each_message():
    w = FakeWatcher([make_message("m1"), make_message("m2")])
    p = FakeProcessor(should_save=False)
    d = EmailWatchDaemon(w, p, mailbox="kb@example.com", use_threads=False)

    res = await d.run_once()

    assert res["processed"] == 2
    assert res["submitted"] == 0
    assert p.processed_messages == 2


@pytest.mark.asyncio
async def test_dedup_across_polls_skips_known_messages():
    w = FakeWatcher([make_message("m1")])
    p = FakeProcessor()
    d = EmailWatchDaemon(w, p, mailbox="kb@example.com")

    await d.run_once()
    # Same message returned again (e.g. not marked read) must be skipped
    w.messages = [make_message("m1")]
    res = await d.run_once()

    assert res["fetched"] == 1
    assert res["processed"] == 0
    assert res["skipped"] == 1
    assert p.processed_threads == 1  # only processed once


@pytest.mark.asyncio
async def test_mark_read_only_when_enabled():
    w = FakeWatcher([make_message("m1")])
    p = FakeProcessor()
    d = EmailWatchDaemon(w, p, mailbox="kb@example.com", mark_read=True)

    await d.run_once()

    assert w.marked == ["m1"]


@pytest.mark.asyncio
async def test_no_mark_read_by_default():
    w = FakeWatcher([make_message("m1")])
    p = FakeProcessor()
    d = EmailWatchDaemon(w, p, mailbox="kb@example.com")

    await d.run_once()

    assert w.marked == []


@pytest.mark.asyncio
async def test_poll_error_does_not_raise():
    class BoomWatcher(FakeWatcher):
        async def fetch_unread(self, mailbox, max_results):
            raise RuntimeError("graph down")

    d = EmailWatchDaemon(BoomWatcher([]), FakeProcessor(), mailbox="kb@example.com")
    res = await d.run_once()  # must not raise
    assert res["fetched"] == 0
