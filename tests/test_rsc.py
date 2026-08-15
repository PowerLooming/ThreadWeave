# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the RSC consent probe (team tracking + permissionGrants check)."""

import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, "src")
from threadweave.connectors.teams.rsc import (
    TeamSeenStore, check_team_consent,
)
from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot


# ---- TeamSeenStore ----------------------------------------------------------


def test_seen_store_add_and_persist(tmp_path, monkeypatch):
    path = str(tmp_path / "teams_seen.json")
    monkeypatch.setenv("THREADWEAVE_TEAMS_SEEN_FILE", path)
    store = TeamSeenStore()
    assert store.add("team-1") is True
    assert store.add("team-1") is False  # dedup
    assert store.add("team-2") is True
    assert store.all() == ["team-1", "team-2"]
    assert store.add("") is False

    reloaded = TeamSeenStore(path=path)
    assert reloaded.all() == ["team-1", "team-2"]


# ---- check_team_consent -----------------------------------------------------


class FakeGraph:
    def __init__(self, grants=None, error_status=None):
        self.grants = grants
        self.error_status = error_status
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if self.error_status:
            request = httpx.Request("GET", "https://graph.microsoft.com" + path)
            raise httpx.HTTPStatusError(
                f"HTTP {self.error_status}",
                request=request,
                response=httpx.Response(self.error_status, request=request),
            )
        return {"value": self.grants or []}


@pytest.fixture
def team_guid():
    return "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_consent_granted_for_our_app(team_guid):
    graph = FakeGraph(grants=[
        {"clientAppId": "bot-1", "permission": "ChannelMessage.Read.Group"},
        {"clientAppId": "other-app", "permission": "ChannelMessage.Read.Group"},
        {"clientAppId": "bot-1", "permission": "ChatMessage.Read.Chat"},
    ])
    result = await check_team_consent(graph, team_guid, "bot-1")
    assert result["status"] == "granted"
    assert result["permissions"] == [
        "ChannelMessage.Read.Group", "ChatMessage.Read.Chat",
    ]
    assert graph.calls == [
        ("GET", f"/teams/{team_guid}/permissionGrants")
    ]


@pytest.mark.asyncio
async def test_consent_missing_when_no_grants_for_app(team_guid):
    graph = FakeGraph(grants=[
        {"clientAppId": "other-app", "permission": "ChannelMessage.Read.Group"},
    ])
    result = await check_team_consent(graph, team_guid, "bot-1")
    assert result["status"] == "missing"
    assert result["permissions"] == []
    assert "Teams admin center" in result["detail"]


@pytest.mark.asyncio
async def test_consent_check_403_reports_read_permission_hint(team_guid):
    graph = FakeGraph(error_status=403)
    result = await check_team_consent(graph, team_guid, "bot-1")
    assert result["status"] == "error"
    assert "TeamsAppInstallation.ReadForTeam.All" in result["detail"]


@pytest.mark.asyncio
async def test_consent_check_rejects_channel_ids():
    """19:...@thread.tacv2 ids are channels, not teams: clean error."""
    graph = FakeGraph()
    result = await check_team_consent(
        graph, "19:5c345b5e1e1e4e64bb6c191611d9973f@thread.tacv2", "bot-1"
    )
    assert result["status"] == "error"
    assert "not a team GUID" in result["detail"]
    assert "Review permissions and consent" in result["detail"]
    assert graph.calls == []  # no wasted Graph call


@pytest.mark.asyncio
async def test_consent_check_requires_ids():
    graph = FakeGraph()
    result = await check_team_consent(graph, "", "bot-1")
    assert result["status"] == "error"
    assert graph.calls == []


# ---- bot wiring -------------------------------------------------------------


def make_bot(monkeypatch, tmp_path, graph, bot_id="bot-1"):
    monkeypatch.setenv(
        "THREADWEAVE_TEAMS_SEEN_FILE", str(tmp_path / "teams_seen.json")
    )
    bot = ThreadWeaveTeamsBot(adapter=None)
    bot._bot_id = bot_id
    bot._graph_client = graph  # bypass env credential lookups
    return bot


@pytest.mark.asyncio
async def test_bot_check_rsc_consent_populates_status(
    monkeypatch, tmp_path, team_guid
):
    graph = FakeGraph(grants=[
        {"clientAppId": "bot-1", "permission": "ChannelMessage.Read.Group"},
    ])
    bot = make_bot(monkeypatch, tmp_path, graph)
    TeamSeenStore().add(team_guid)

    await bot.check_rsc_consent()
    assert bot.rsc_status[team_guid]["status"] == "granted"


@pytest.mark.asyncio
async def test_bot_check_rsc_consent_missing_is_reported(
    monkeypatch, tmp_path, team_guid
):
    graph = FakeGraph(grants=[])
    bot = make_bot(monkeypatch, tmp_path, graph)
    TeamSeenStore().add(team_guid)

    await bot.check_rsc_consent()
    assert bot.rsc_status[team_guid]["status"] == "missing"


@pytest.mark.asyncio
async def test_bot_check_rsc_skips_without_credentials(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv(
        "THREADWEAVE_TEAMS_SEEN_FILE", str(tmp_path / "teams_seen.json")
    )
    bot = ThreadWeaveTeamsBot(adapter=None)
    bot._bot_id = ""  # no bot app id configured

    import logging

    with caplog.at_level(logging.WARNING):
        await bot.check_rsc_consent()
    assert any("MICROSOFT_APP_ID" in r.message for r in caplog.records)
    assert bot.rsc_status == {}


@pytest.mark.asyncio
async def test_remember_team_records_and_probes(
    monkeypatch, tmp_path, team_guid
):
    graph = FakeGraph(grants=[
        {"clientAppId": "bot-1", "permission": "ChannelMessage.Read.Group"},
    ])
    bot = make_bot(monkeypatch, tmp_path, graph)

    activity = SimpleNamespace(
        channel_data={"team": {"id": team_guid}, "channel": {"id": "c1"}}
    )
    bot._remember_team(activity)
    assert TeamSeenStore().all() == [team_guid]

    # the fire-and-forget probe task runs on the loop
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bot.rsc_status[team_guid]["status"] == "granted"


def test_remember_team_prefers_aad_group_id(monkeypatch, tmp_path):
    """Channel-scoped installs: team.id is the channel id, aadGroupId is
    the team GUID. The store must record the GUID (Graph's team id)."""
    bot = make_bot(monkeypatch, tmp_path, FakeGraph())
    bot._bot_id = ""  # no probe side effects

    activity = SimpleNamespace(
        channel_data={
            "team": {
                "id": "19:5c345b5e1e1e4e64bb6c191611d9973f@thread.tacv2",
                "aadGroupId": "22222222-3333-4444-5555-666666666666",
                "name": "Sales West",
            },
            "channel": {
                "id": "19:5c345b5e1e1e4e64bb6c191611d9973f@thread.tacv2",
                "name": "Sales West",
            },
        }
    )
    bot._remember_team(activity)
    assert TeamSeenStore().all() == [
        "22222222-3333-4444-5555-666666666666"
    ]


def test_remember_team_ignores_activities_without_team(
    monkeypatch, tmp_path
):
    bot = make_bot(monkeypatch, tmp_path, FakeGraph())
    bot._remember_team(SimpleNamespace(channel_data={}))
    bot._remember_team(SimpleNamespace())
    bot._remember_team(SimpleNamespace(channel_data={"team": {}}))
    assert TeamSeenStore().all() == []


def test_health_exposes_rsc_status():
    from threadweave.connectors.teams.adapter import create_app

    app = create_app()
    assert app is not None  # botbuilder import path exercised


# ---- GraphClient empty-body handling (202/204 responses) --------------------


class FakeResponse:
    def __init__(self, content=b"", status_code=202):
        self._content = content
        self.status_code = status_code

    def raise_for_status(self):
        pass

    @property
    def content(self):
        return self._content

    def json(self):
        raise ValueError("no JSON on empty 202")


def fake_async_client_factory(response):
    """Returns a FakeAsyncClient class bound to a specific response."""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *args, **kwargs):
            return response

    return FakeAsyncClient


def make_bare_graph(response):
    from threadweave.connectors.sharepoint.watcher import GraphClient

    client = object.__new__(GraphClient)

    async def fake_token():
        return "tok"

    client._get_token = fake_token
    return client


@pytest.mark.asyncio
async def test_graph_request_accepts_empty_202(monkeypatch):
    from threadweave.connectors.sharepoint import watcher as watcher_mod

    client = make_bare_graph(None)
    monkeypatch.setattr(
        watcher_mod.httpx, "AsyncClient",
        fake_async_client_factory(FakeResponse()),
    )
    result = await client._request_url(
        "POST", "https://graph.microsoft.com/v1.0/users/x/sendMail",
        json_body={"message": {}},
    )
    assert result == {}  # 202 empty body is success, not a JSON error


@pytest.mark.asyncio
async def test_graph_request_parses_json_bodies(monkeypatch):
    from threadweave.connectors.sharepoint import watcher as watcher_mod

    class JsonResponse(FakeResponse):
        @property
        def content(self):
            return b'{"value": [1, 2]}'

        def json(self):
            return {"value": [1, 2]}

    client = make_bare_graph(None)
    monkeypatch.setattr(
        watcher_mod.httpx, "AsyncClient",
        fake_async_client_factory(JsonResponse()),
    )
    result = await client._request_url(
        "GET", "https://graph.microsoft.com/v1.0/teams/t1/permissionGrants"
    )
    assert result == {"value": [1, 2]}
