# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Bot search command tests."""

import pytest

from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot


class FakeTurnContext:
    def __init__(self):
        self.sent = []

    async def send_activity(self, text):
        self.sent.append(text)


class SearchBot(ThreadWeaveTeamsBot):
    def __init__(self, results):
        super().__init__(adapter=None)
        self._results = results
        self._queries = []

    async def _api_search(self, query):
        self._queries.append(query)
        return self._results


def test_search_command_replies_with_results(monkeypatch):
    bot = SearchBot(results=[
        {
            "title": "Q3 pricing",
            "wing": "u.s._sales",
            "room": "sales_west",
            "content_preview": "We decided to use the Q3 pricing model",
            "created_at": "2026-08-16T10:42:20+00:00",
        },
    ])
    ctx = FakeTurnContext()

    import asyncio
    asyncio.run(bot._handle_search_command(ctx, "pricing"))

    assert bot._queries == ["pricing"]
    assert len(ctx.sent) == 1
    reply = ctx.sent[0]
    assert "1 match(es) for 'pricing'" in reply
    assert "Q3 pricing" in reply
    assert "u.s._sales/sales_west" in reply


def test_search_command_no_results(monkeypatch):
    bot = SearchBot(results=[])
    ctx = FakeTurnContext()

    import asyncio
    asyncio.run(bot._handle_search_command(ctx, "nothing"))

    assert ctx.sent == ["No palace entries matched 'nothing'."]


def test_search_command_caps_at_eight(monkeypatch):
    results = [
        {"title": f"entry {i}", "wing": "w", "room": "r",
         "content_preview": "x", "created_at": "2026-08-17T00:00:00+00:00"}
        for i in range(12)
    ]
    bot = SearchBot(results=results)
    ctx = FakeTurnContext()

    import asyncio
    asyncio.run(bot._handle_search_command(ctx, "x"))

    reply = ctx.sent[0]
    assert "8 match(es)" in reply
    assert "+4 more" in reply


def test_search_command_requires_query(monkeypatch):
    bot = SearchBot(results=[])
    ctx = FakeTurnContext()

    import asyncio
    asyncio.run(bot._handle_search_command(ctx, ""))

    assert "What should I search for?" in ctx.sent[0]
    assert bot._queries == []


def test_strip_mention_variants():
    from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot

    assert ThreadWeaveTeamsBot._strip_mention(
        "@ThreadWeave search postgresql"
    ) == "search postgresql"
    assert ThreadWeaveTeamsBot._strip_mention(
        "<at>ThreadWeave</at> search postgresql"
    ) == "search postgresql"
    assert ThreadWeaveTeamsBot._strip_mention(
        "search postgresql"
    ) == "search postgresql"
    assert ThreadWeaveTeamsBot._strip_mention(
        "@threadweave status"
    ) == "status"


@pytest.mark.asyncio
async def test_search_command_works_with_mention_prefix():
    """Channel/group-chat mentions arrive with the @name in the text;
    the parser must not depend on the prefix (live 2026-08-17)."""
    from types import SimpleNamespace

    bot = SearchBot(results=[
        {"title": "PostgreSQL store", "wing": "engineering",
         "room": "decision", "content_preview": "We decided...",
         "created_at": "2026-08-07T21:46:51+00:00"},
    ])
    ctx = FakeTurnContext()
    activity = SimpleNamespace(
        from_property=SimpleNamespace(id="u1", aad_object_id="u1"),
    )

    handled = await bot._handle_privacy_command(
        ctx, activity, "@ThreadWeave search postgresql"
    )
    assert handled is True
    assert bot._queries == ["postgresql"]
    assert "PostgreSQL store" in ctx.sent[0]
