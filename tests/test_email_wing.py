# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for email sender -> department -> wing mapping (palace model)."""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.email.processor import EmailProcessor


class FakeGraph:
    """Graph client stub returning departments per user."""

    def __init__(self, departments: dict):
        self.departments = departments
        self.calls = []

    async def _request(self, method, path, params=None):
        self.calls.append(path)
        email = path.split("/users/")[-1]
        return {"department": self.departments.get(email), "userPrincipalName": email}


@pytest.mark.asyncio
async def test_resolve_wing_uses_department():
    graph = FakeGraph({"adele@x.com": "Retail"})
    proc = EmailProcessor(graph_client=graph)
    wing = await proc._resolve_wing("adele@x.com")
    assert wing == "Retail"


@pytest.mark.asyncio
async def test_resolve_wing_falls_back_to_email():
    graph = FakeGraph({"admin@x.com": None})  # no department set
    proc = EmailProcessor(graph_client=graph)
    wing = await proc._resolve_wing("admin@x.com")
    assert wing == "email"


@pytest.mark.asyncio
async def test_resolve_wing_without_graph_uses_email():
    proc = EmailProcessor()  # no graph client
    wing = await proc._resolve_wing("anyone@x.com")
    assert wing == "email"


@pytest.mark.asyncio
async def test_resolve_wing_caches_lookups():
    graph = FakeGraph({"adele@x.com": "Retail"})
    proc = EmailProcessor(graph_client=graph)
    w1 = await proc._resolve_wing("adele@x.com")
    w2 = await proc._resolve_wing("adele@x.com")
    assert w1 == w2 == "Retail"
    assert len(graph.calls) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_resolve_wing_handles_graph_errors():
    class BoomGraph:
        async def _request(self, method, path, params=None):
            raise RuntimeError("graph down")

    proc = EmailProcessor(graph_client=BoomGraph())
    wing = await proc._resolve_wing("adele@x.com")
    assert wing == "email"  # graceful fallback


@pytest.mark.asyncio
async def test_resolve_wing_falls_back_to_recipient_department():
    # Sender has no department; recipient (Adele) is in Retail
    graph = FakeGraph({"admin@x.com": None, "adele@x.com": "Retail"})
    proc = EmailProcessor(graph_client=graph)
    wing = await proc._resolve_wing("admin@x.com", recipients=["adele@x.com"])
    assert wing == "Retail"


@pytest.mark.asyncio
async def test_resolve_wing_sender_wins_over_recipients():
    # Sender has a department -> wins even if recipients have one too
    graph = FakeGraph({"patti@x.com": "Executive Management", "adele@x.com": "Retail"})
    proc = EmailProcessor(graph_client=graph)
    wing = await proc._resolve_wing("patti@x.com", recipients=["adele@x.com"])
    assert wing == "Executive Management"


@pytest.mark.asyncio
async def test_resolve_wing_unknown_recipients_fall_back_to_email():
    graph = FakeGraph({})  # nobody resolvable
    proc = EmailProcessor(graph_client=graph)
    wing = await proc._resolve_wing("admin@x.com", recipients=["someone@external.com"])
    assert wing == "email"


@pytest.mark.asyncio
async def test_resolve_wing_cached_unknown_sender_does_not_block_recipients():
    # Regression: after Admin is cached as "email" (no department), a second
    # lookup with Adele as recipient must STILL resolve to Retail — a cached
    # "email" (unknown) must not short-circuit the recipient fallback.
    graph = FakeGraph({"adele@x.com": "Retail"})
    proc = EmailProcessor(graph_client=graph)

    wing1 = await proc._resolve_wing("admin@x.com", recipients=["adele@x.com"])
    assert wing1 == "Retail"  # first lookup: sender unknown, recipient wins

    wing2 = await proc._resolve_wing("admin@x.com", recipients=["adele@x.com"])
    assert wing2 == "Retail"  # second lookup: Admin cached as email, no short-circuit


@pytest.mark.asyncio
async def test_resolve_wing_cached_real_wing_short_circuits():
    graph = FakeGraph({"patti@x.com": "Executive Management"})
    proc = EmailProcessor(graph_client=graph)

    await proc._resolve_wing("patti@x.com", recipients=[])
    # Cached real wing is returned without re-querying
    wing = await proc._resolve_wing("patti@x.com", recipients=["adele@x.com"])
    assert wing == "Executive Management"
    assert len(graph.calls) == 1  # only the first lookup hit Graph
