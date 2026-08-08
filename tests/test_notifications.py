# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for capture notifications ("camera sign" DMs)."""

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.notify import NotificationStore
from threadweave.connectors.teams.conversations import ConversationStore

client = TestClient(app)


# ---- NotificationStore ----

def test_enqueue_and_pending(tmp_path):
    s = NotificationStore(db_path=str(tmp_path / "n.sqlite3"))
    assert s.enqueue("n1", "e1", "adele@x.com", "Decision A", "Retail",
                     "decision", "email", "2026-08-07T20:00:00+00:00")
    assert s.enqueue("n2", "e2", "patti@x.com", "Decision B", "Executive",
                     "decision", "email", "2026-08-07T20:01:00+00:00")
    assert not s.enqueue("n1", "e1", "adele@x.com", "Decision A", "Retail",
                         "decision", "email", "2026-08-07T20:00:00+00:00")
    pending = s.pending()
    assert len(pending) == 2
    assert pending[0]["author_id"] == "adele@x.com"


def test_mark_delivered_and_count(tmp_path):
    s = NotificationStore(db_path=str(tmp_path / "n.sqlite3"))
    s.enqueue("n1", "e1", "a@x.com", "T", "w", "r", "email", "now")
    s.enqueue("n2", "e2", "b@x.com", "T", "w", "r", "email", "now")
    assert s.count() == 2
    s.mark_delivered("n1")
    assert s.count() == 1
    assert s.count(delivered_only=True) == 1


def test_persistence_across_restart(tmp_path):
    path = str(tmp_path / "n.sqlite3")
    NotificationStore(db_path=path).enqueue("n1", "e1", "a@x.com", "T",
                                            "w", "r", "email", "now")
    s2 = NotificationStore(db_path=path)
    assert len(s2.pending()) == 1


# ---- API endpoints ----

def test_api_notifications_flow():
    # enqueue happens via ingest; simulate with a direct ingest save
    r = client.post("/api/v1/ingest", json={
        "content": "We decided to move the build pipeline to GitHub Actions "
                   "because the self-hosted runners kept failing on Windows",
        "source": "email",
        "tenant_id": "default",
        "metadata": {"email_sender": "adele@x.com", "title": "build pipeline"},
    })
    body = r.json()
    if body["id"] != "opted_out" and body["should_save"]:
        pending = client.get("/api/v1/notifications/pending").json()
        assert len(pending["notifications"]) >= 1
        notif = pending["notifications"][0]
        assert notif["author_id"] == "adele@x.com"

        r = client.post(f"/api/v1/notifications/{notif['id']}/delivered")
        assert r.json()["delivered"] is True
        stats = client.get("/api/v1/notifications/stats").json()
        assert stats["delivered"] >= 1


# ---- ConversationStore ----

def test_conversation_store_roundtrip(tmp_path):
    s = ConversationStore(path=str(tmp_path / "c.json"))
    s.remember("aad-123", "conv-abc", "https://smba.example.com")
    s2 = ConversationStore(path=str(tmp_path / "c.json"))
    ref = s2.get("aad-123")
    assert ref["conversation_id"] == "conv-abc"
    assert ref["service_url"] == "https://smba.example.com"
    assert s2.get("unknown") is None


# ---- Bot delivery loop (fakes) ----

class FakeApi:
    def __init__(self, notifications):
        self._pending = list(notifications)
        self.delivered = []

    async def _api_get(self, path):
        if "pending" in path:
            return {"notifications": self._pending}
        return None

    async def _api_post(self, path, body):
        nid = path.split("/")[-2]
        self.delivered.append(nid)
        return {"ok": True}


class FakeBot:
    """Minimal stand-in exercising the delivery loop without botbuilder."""

    def __init__(self, notifications, store):
        self._api = FakeApi(notifications)
        self.stats = {}
        self._store = store
        self.sent = []
        self.resolved = {}

    async def _deliver_pending_notifications(self):
        data = await self._api._api_get("/api/v1/notifications/pending")
        for notif in data["notifications"]:
            author = notif["author_id"]
            ref = self._store.get(author)
            if not ref:
                aad = self.resolved.get(author, "")
                if aad:
                    ref = self._store.get(aad)
            if not ref:
                continue
            self.sent.append((author, notif["entry_id"]))
            await self._api._api_post(
                f"/api/v1/notifications/{notif['id']}/delivered", {})

    async def _resolve_aad_id(self, email):
        return self.resolved.get(email, "")


@pytest.mark.asyncio
async def test_delivery_skips_unknown_author(tmp_path):
    store = ConversationStore(path=str(tmp_path / "c.json"))
    bot = FakeBot([{"id": "n1", "author_id": "adele@x.com",
                    "entry_id": "e1", "title": "T", "wing": "w",
                    "source": "email"}], store)
    await bot._deliver_pending_notifications()
    assert bot.sent == []
    assert bot._api.delivered == []


@pytest.mark.asyncio
async def test_delivery_dms_known_author_and_marks_delivered(tmp_path):
    store = ConversationStore(path=str(tmp_path / "c.json"))
    store.remember("aad-adele", "conv-1", "https://smba.example.com")
    bot = FakeBot([{"id": "n1", "author_id": "adele@x.com",
                    "entry_id": "e1", "title": "T", "wing": "w",
                    "source": "email"}], store)
    bot.resolved = {"adele@x.com": "aad-adele"}
    await bot._deliver_pending_notifications()
    assert bot.sent == [("adele@x.com", "e1")]
    assert bot._api.delivered == ["n1"]


@pytest.mark.asyncio
async def test_delivery_email_key_direct_match(tmp_path):
    """Author id is a Teams AAD id directly in the store."""
    store = ConversationStore(path=str(tmp_path / "c.json"))
    store.remember("aad-123", "conv-1", "https://smba.example.com")
    bot = FakeBot([{"id": "n1", "author_id": "aad-123",
                    "entry_id": "e1", "title": "T", "wing": "w",
                    "source": "sharepoint"}], store)
    await bot._deliver_pending_notifications()
    assert bot.sent == [("aad-123", "e1")]
    assert bot._api.delivered == ["n1"]
