# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the email capture citation (P2): the Outlook deep link must
survive from the Graph message to the entry's source_url.

Regression: the watcher never asked Graph for webLink and never forwarded a
link, so every mail capture came out with an empty citation even though the
message it came from was known.
"""

import sys

import pytest

sys.path.insert(0, "src")
from threadweave.api import _citation_url
from threadweave.connectors.email.processor import EmailProcessor
from threadweave.connectors.email.watcher import MailWatcher

WEB_LINK = (
    "https://outlook.office365.com/owa/?ItemID=AAMkADU0ZTk5ZWE1&exvsurl=1"
    "&viewmodel=ReadMessageItem"
)


def watcher():
    """A MailWatcher without its GraphClient.

    Constructing one builds an msal client that resolves the tenant against
    the network, so the tests skip __init__ and inject a stub .graph instead.
    Nothing in _parse_message needs instance state."""
    w = MailWatcher.__new__(MailWatcher)
    w.graph = None
    w.delegated = False   # app-only mode, as in the managed tier
    return w


class FakeGraph:
    """Captures request params and returns one message."""

    def __init__(self, item):
        self.item = item
        self.params = []

    async def _request(self, method, path, params=None):
        self.params.append(params or {})
        return {"value": [self.item]}


def graph_item(**over):
    item = {
        "id": "m1",
        "conversationId": "c1",
        "subject": "Vendor onboarding checklist",
        "from": {"emailAddress": {"name": "Harald", "address": "h@x.com"}},
        "toRecipients": [{"emailAddress": {"address": "kb@x.com"}}],
        "body": {"contentType": "text", "content": "We decided to standardise."},
        "hasAttachments": False,
        "receivedDateTime": "2026-09-27T13:29:04Z",
        "isRead": False,
        "importance": "normal",
        "webLink": WEB_LINK,
    }
    item.update(over)
    return item


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "e1", "should_save": True}


class FakeAsyncClient:
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        FakeAsyncClient.last_json = json
        return FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    import httpx

    FakeAsyncClient.last_json = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def test_parse_message_keeps_the_web_link():
    assert watcher()._parse_message(graph_item()).web_link == WEB_LINK


def test_parse_message_tolerates_a_missing_web_link():
    item = graph_item()
    item.pop("webLink")
    assert watcher()._parse_message(item).web_link == ""


@pytest.mark.asyncio
async def test_fetch_unread_asks_graph_for_web_link():
    w = watcher()
    w.graph = FakeGraph(graph_item())
    messages = await w.fetch_unread("kb@x.com")
    assert "webLink" in w.graph.params[0]["$select"]
    assert messages[0].web_link == WEB_LINK


@pytest.mark.asyncio
async def test_fetch_thread_asks_graph_for_web_link():
    w = watcher()
    w.graph = FakeGraph(graph_item())
    await w.fetch_thread("kb@x.com", "c1")
    assert "webLink" in w.graph.params[0]["$select"]


@pytest.mark.asyncio
async def test_ingest_payload_carries_the_message_url(fake_http):
    proc = EmailProcessor()  # no graph client: wing falls back to "email"

    await proc._mine_to_mempalace(
        text="We decided to standardise the checklist.",
        subject="Vendor onboarding checklist",
        sender="h@x.com",
        conversation_id="c1",
        participants=["h@x.com"],
        received_at="2026-09-27T13:29:04Z",
        content_type="decision",
        scope="team",
        thread_messages=1,
        web_link=WEB_LINK,
    )

    metadata = fake_http.last_json["metadata"]
    assert metadata["message_url"] == WEB_LINK
    # the citation the API reports resolves to that link
    assert _citation_url(metadata) == WEB_LINK


@pytest.mark.asyncio
async def test_ingest_payload_without_a_link_has_no_message_url(fake_http):
    proc = EmailProcessor()

    await proc._mine_to_mempalace(
        text="We decided to standardise the checklist.",
        subject="Vendor onboarding checklist",
        sender="h@x.com",
        conversation_id="c1",
        participants=["h@x.com"],
        received_at="2026-09-27T13:29:04Z",
    )

    metadata = fake_http.last_json["metadata"]
    assert "message_url" not in metadata
    assert _citation_url(metadata) == ""


@pytest.mark.asyncio
async def test_the_link_travels_from_message_to_payload(fake_http):
    """Whole path: Graph item -> EmailMessage -> process_message -> ingest."""
    msg = watcher()._parse_message(graph_item())
    assert msg.web_link == WEB_LINK

    result = await EmailProcessor().process_message(msg)
    if result.should_save:
        assert fake_http.last_json["metadata"]["message_url"] == WEB_LINK
    else:
        # detection declined the body, so nothing was submitted
        assert fake_http.last_json is None
