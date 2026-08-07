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
