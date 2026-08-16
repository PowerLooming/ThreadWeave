# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the Teams watch daemon (Graph channel-message delta polling)."""

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.teams.watcher import (
    TeamsGraphClient, TeamsWatchDaemon,
)
from threadweave.daemons import DAEMONS


# ---- helpers ---------------------------------------------------------------


def zulu(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_zulu(offset_seconds=0):
    return zulu(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))


def make_msg(
    msg_id="m1",
    text="We decided to use PostgreSQL for the durable entry store going forward.",
    author_id="user1",
    message_type="message",
    from_application=None,
    created=None,
    subject="",
    channel_id="ch1",
    team_id="team1",
):
    msg = {
        "id": msg_id,
        "messageType": message_type,
        "createdDateTime": created or now_zulu(-3600),
        "body": {"content": f"<div>{text}</div>"},
        "webUrl": (
            f"https://teams.microsoft.com/l/message/{channel_id}/{msg_id}"
        ),
        "from": {"user": {"id": author_id, "displayName": "Jane"}},
    }
    if from_application is not None:
        msg["from"] = from_application
    if subject:
        msg["subject"] = subject
    return msg


class FakeGraph:
    """Scripted TeamsGraphClient: teams/channels + delta responses."""

    def __init__(self):
        self.team = {"id": "team1", "displayName": "Mark 8 Project Team"}
        self.channel = {"id": "ch1", "displayName": "Decisions"}
        self.polls = []  # (team_id, channel_id, delta_url) per call
        self.history = []  # messages returned when no delta token
        self.new_messages = []  # messages returned when a token exists
        self.delta_links = [
            "https://graph.microsoft.com/v1.0/teams/team1/channels/ch1/messages/delta?token=abc",
            "https://graph.microsoft.com/v1.0/teams/team1/channels/ch1/messages/delta?token=def",
        ]

    async def list_teams(self):
        from types import SimpleNamespace
        return [SimpleNamespace(
            id=self.team["id"], display_name=self.team["displayName"],
        )]

    async def list_channels(self, team_id):
        from types import SimpleNamespace
        return [SimpleNamespace(
            team_id=team_id, team_name="", id=self.channel["id"],
            display_name=self.channel["displayName"],
        )]

    async def get_channel_messages_delta(self, team_id, channel_id, delta_url=""):
        self.polls.append((team_id, channel_id, delta_url))
        if delta_url:
            return list(self.new_messages), self.delta_links[1]
        return list(self.history), self.delta_links[0]


class FakeSink:
    """Captures ingest payloads instead of POSTing to the API."""

    def __init__(self, should_save=True):
        self.payloads = []
        self.should_save = should_save

    async def __call__(self, payload):
        self.payloads.append(payload)
        return {"id": f"entry-{len(self.payloads)}",
                "should_save": self.should_save}


class StubOptOut:
    def __init__(self, opted=None):
        self._opted = set(opted or [])

    def any_opted_out(self, persons):
        return any(p in self._opted for p in persons)


@pytest.fixture
def opted_out_author(monkeypatch):
    monkeypatch.setattr(
        "threadweave.optout.OptOutStore",
        lambda: StubOptOut(opted=["user-opted-out"]),
    )


# ---- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_prime_mode_skips_history_and_captures_new(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    graph.history = [
        make_msg("old1", created=now_zulu(-86400)),
        make_msg("old2", created=now_zulu(-7200)),
    ]
    graph.new_messages = [
        make_msg("new1", created=now_zulu(+30),
                 text="We will deploy the daemon as a Windows startup task."),
    ]
    sink = FakeSink()
    state = str(tmp_path / "state.json")
    daemon = TeamsWatchDaemon(graph, state_file=state, ingest=sink)

    first = await daemon.run_once()
    assert sink.payloads == []  # history is primed, not processed
    assert first["messages_seen"] == 2

    second = await daemon.run_once()
    assert len(sink.payloads) == 1
    assert sink.payloads[0]["content"].startswith("We will deploy")
    assert second["submitted"] == 1


@pytest.mark.asyncio
async def test_backfill_processes_history_with_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    graph.history = [
        make_msg(f"h{i}") for i in range(5)
    ]
    sink = FakeSink()
    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=sink,
        backfill=True, max_messages=3,
    )

    await daemon.run_once()
    assert len(sink.payloads) == 3  # capped at the 3 newest


@pytest.mark.asyncio
async def test_skip_rules(tmp_path, monkeypatch, opted_out_author):
    graph = FakeGraph()
    graph.history = [
        make_msg("sys1", message_type="systemEventMessage"),
        make_msg("bot1", from_application={"id": "bot-app"}),
        make_msg("anon1", author_id=""),
        make_msg("opt1", author_id="user-opted-out"),
        make_msg("short1", text="+1"),
        make_msg("good1"),
    ]
    sink = FakeSink()
    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=sink,
        backfill=True,
    )

    res = await daemon.run_once()
    assert len(sink.payloads) == 1
    assert sink.payloads[0]["metadata"]["source_file"] == "good1"
    assert res["skipped"] == 5


@pytest.mark.asyncio
async def test_short_decision_not_dropped_by_watcher(
    tmp_path, monkeypatch
):
    """A 48-char explicit decision passes the watcher (only the noise
    floor filters) and reaches the sink; the central detector decides
    (live 2026-08-16: the 50-char watcher gate dropped these)."""
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    graph.history = [
        make_msg(
            "short_decision",
            text="We decided to review the pricing pages quarterly",
            author_id="aad-user-1",
        ),
    ]
    sink = FakeSink()
    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=sink,
        backfill=True,
    )

    res = await daemon.run_once()
    assert res["skipped"] == 0
    assert len(sink.payloads) == 1
    assert sink.payloads[0]["metadata"]["source_file"] == "short_decision"


@pytest.mark.asyncio
async def test_metadata_mapping_and_html_strip(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    graph.history = [
        make_msg(
            "m1",
            text=(
                "Use <b>uv run</b> &amp; the daemon <i>envelope</i> "
                "for all python tooling from now on."
            ),
            author_id="aad-user-1",
            subject="RE: Python toolchain",
            created="2026-08-15T10:00:00.000Z",
        ),
    ]
    sink = FakeSink()
    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=sink,
        backfill=True,
    )

    await daemon.run_once()
    meta = sink.payloads[0]["metadata"]
    assert sink.payloads[0]["source"] == "teams"
    assert sink.payloads[0]["content"] == (
        "Use uv run & the daemon envelope for all python tooling from now on."
    )
    assert meta["wing"] == "mark_8_project_team"
    assert meta["room"] == "decisions"
    assert meta["author_id"] == "aad-user-1"
    assert meta["title"] == "RE: Python toolchain"
    assert meta["source_file"] == "m1"
    assert meta["created_at"] == "2026-08-15T10:00:00.000Z"
    assert meta["message_url"].startswith("https://teams.microsoft.com")


@pytest.mark.asyncio
async def test_delta_token_persisted_and_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    state = str(tmp_path / "state.json")

    graph1 = FakeGraph()
    graph1.history = [make_msg("m1")]
    daemon1 = TeamsWatchDaemon(graph1, state_file=state, ingest=FakeSink())
    await daemon1.run_once()
    # token persisted after the first poll
    with open(state, encoding="utf-8") as fh:
        persisted = __import__("json").load(fh)
    assert "team1/ch1" in persisted
    assert persisted["team1/ch1"].endswith("token=abc")

    # A fresh daemon resumes from the token: history is NOT re-read.
    graph2 = FakeGraph()
    graph2.history = [make_msg("m1-again")]  # would reprocess if re-crawled
    sink2 = FakeSink()
    daemon2 = TeamsWatchDaemon(graph2, state_file=state, ingest=sink2)
    await daemon2.run_once()
    assert sink2.payloads == []
    assert graph2.polls[-1][2].endswith("token=abc")


@pytest.mark.asyncio
async def test_team_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    sink = FakeSink()
    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=sink,
        backfill=True, team_filter="other team",
    )
    res = await daemon.run_once()
    assert res["teams"] == 0
    assert sink.payloads == []


@pytest.mark.asyncio
async def test_ingest_failure_counts_error_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr("threadweave.optout.OptOutStore", lambda: StubOptOut())
    graph = FakeGraph()
    graph.history = [make_msg("m1"), make_msg("m2")]

    class Boom:
        async def __call__(self, payload):
            raise RuntimeError("API down")

    daemon = TeamsWatchDaemon(
        graph, state_file=str(tmp_path / "state.json"), ingest=Boom(),
        backfill=True,
    )
    res = await daemon.run_once()
    assert res["errors"] == 2
    assert res["processed"] == 0


def test_delta_pagination_follows_nextlink():
    """The client follows @odata.nextLink pages and returns the deltaLink."""
    client = object.__new__(TeamsGraphClient)
    pages = [
        {"value": [{"id": "a"}], "@odata.nextLink": "https://g/p2"},
        {"value": [{"id": "b"}, {"id": "c"}],
         "@odata.deltaLink": "https://g/delta?token=t"},
    ]
    calls = []

    async def fake_request_url(method, url):
        calls.append(url)
        return pages.pop(0)

    client._request_url = fake_request_url

    import asyncio

    msgs, delta = asyncio.run(
        client.get_channel_messages_delta("t", "c")
    )
    assert [m["id"] for m in msgs] == ["a", "b", "c"]
    assert delta == "https://g/delta?token=t"
    assert len(calls) == 2


def _delta_token_error():
    import httpx

    request = httpx.Request("GET", "https://g/delta")
    response = httpx.Response(
        400, request=request,
        json={"error": {
            "code": "BadRequest",
            "message": "Parameter 'DeltaToken' not supported for this request.",
        }},
    )
    return httpx.HTTPStatusError(
        "Client error '400 Bad Request' for url 'https://g/delta'",
        request=request,
        response=response,
    )


def test_delta_tolerates_zero_message_skiptoken_quirk():
    """Live Graph quirk (2026-08-16): empty channels return a nextLink
    with $skiptoken that Graph rejects with 400 'DeltaToken not
    supported'. The client must return what it has without a deltaLink
    and let the next poll re-prime, not raise and break the cycle."""
    client = object.__new__(TeamsGraphClient)
    pages = [
        {"value": [], "@odata.nextLink": "https://g/delta?$skiptoken=xyz"},
    ]
    calls = []

    async def fake_request_url(method, url):
        calls.append(url)
        if len(calls) == 1:
            return pages.pop(0)
        raise _delta_token_error()

    client._request_url = fake_request_url

    import asyncio

    msgs, delta = asyncio.run(
        client.get_channel_messages_delta("t", "c")
    )
    assert msgs == []
    assert delta == ""
    assert len(calls) == 2  # first page fetched, nextLink followed once


def test_delta_raises_on_other_nextlink_errors():
    """Only the DeltaToken quirk is tolerated; other failures propagate."""
    client = object.__new__(TeamsGraphClient)

    async def fake_request_url(method, url):
        import httpx

        request = httpx.Request("GET", url)
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError(
            "Server error '500 Internal Server Error'", request=request,
            response=response,
        )

    client._request_url = fake_request_url

    import pytest

    with pytest.raises(Exception):
        asyncio.run(client.get_channel_messages_delta("t", "c"))


def test_teams_watch_registered_as_daemon():
    assert "teams-watch" in DAEMONS
    assert DAEMONS["teams-watch"]["argv"][-2:] == ["teams", "watch"]
