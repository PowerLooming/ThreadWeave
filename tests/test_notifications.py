# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for capture notifications ("camera sign" DMs)."""

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.notify import NotificationStore
from threadweave.connectors.teams.conversations import ConversationStore
from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot

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


def test_channel_activity_never_overwrites_personal_ref(tmp_path):
    """Org-wide RSC delivers every channel message: a channel activity
    must not clobber a person's 1:1 ref or the DM camera-sign leg dies
    silently (live 2026-08-16)."""
    from types import SimpleNamespace

    def activity(conv_type):
        return SimpleNamespace(
            id=f"act-{conv_type}",
            conversation=SimpleNamespace(conversation_type=conv_type),
            from_property=SimpleNamespace(id="29:user", aad_object_id=""),
            recipient=SimpleNamespace(id="28:bot", name="ThreadWeaveBot"),
        )

    s = ConversationStore(path=str(tmp_path / "c.json"))
    s.remember(
        "aad-123", "personal-conv", "https://smba.example.com",
        activity=activity("personal"),
    )
    s.remember(
        "aad-123", "channel-conv", "https://smba.example.com",
        activity=activity("channel"),
    )
    ref = s.get("aad-123")
    assert ref["conversation_id"] == "personal-conv"
    assert ref["conversation_type"] == "personal"


def test_personal_activity_still_upgrades_channel_ref(tmp_path):
    """The reverse direction IS an upgrade: a DM after channel traffic
    must replace the channel ref."""
    from types import SimpleNamespace

    def activity(conv_type):
        return SimpleNamespace(
            id=f"act-{conv_type}",
            conversation=SimpleNamespace(conversation_type=conv_type),
            from_property=SimpleNamespace(id="29:user", aad_object_id=""),
            recipient=SimpleNamespace(id="28:bot", name="ThreadWeaveBot"),
        )

    s = ConversationStore(path=str(tmp_path / "c.json"))
    s.remember(
        "aad-123", "channel-conv", "https://smba.example.com",
        activity=activity("channel"),
    )
    s.remember(
        "aad-123", "personal-conv", "https://smba.example.com",
        activity=activity("personal"),
    )
    ref = s.get("aad-123")
    assert ref["conversation_id"] == "personal-conv"
    assert ref["conversation_type"] == "personal"


def test_skipped_not_retried_and_counted(tmp_path):
    s = NotificationStore(db_path=str(tmp_path / "n.sqlite3"))
    s.enqueue("n1", "e1", "a@x.com", "T", "w", "r", "email", "now")
    s.mark_delivered("n1", skipped=True)
    assert s.pending() == []
    assert s.count() == 0
    assert s.count(delivered_only=True) == 0  # skipped is not delivered
    assert s.count_skipped() == 1


# ---- Bot delivery loop (real ThreadWeaveTeamsBot, faked IO) ----


class BotUnderTest(ThreadWeaveTeamsBot):
    """Real delivery logic with faked API, resolution, and senders."""

    def __init__(self, notifications, resolved=None, max_attempts=3):
        super().__init__(adapter=None)
        self._pending = list(notifications)
        self.delivered_paths = []
        self.dm_refs = []
        self.activity_targets = []
        self.activity_ok = True
        self.email_sent = []
        self.email_ok = True
        self.email_map = {}
        self._resolved = resolved or {}
        self._notify_max_attempts = max_attempts

    async def _api_get(self, path):
        if "pending" in path:
            return {"notifications": self._pending}
        return None

    async def _api_post(self, path, body):
        self.delivered_paths.append(path)
        return {"ok": True}

    async def _resolve_aad_id(self, email):
        return self._resolved.get(email, "")

    async def _send_capture_notification(self, notif, ref):
        self.dm_refs.append(ref.get("conversation_type"))
        return True

    async def _send_activity_notification(self, notif, aad_id):
        if not aad_id:  # mirror the real method's guard
            return False
        if not self.activity_ok:
            return False
        self.activity_targets.append(aad_id)
        return True

    async def _resolve_author_email(self, author_id):
        if "@" in author_id:
            return author_id
        return self.email_map.get(author_id, "")

    async def _send_email_notification(self, notif, author_email):
        if not self._notify_email_enabled:  # mirror the real guard
            return False
        if not author_email or "@" not in author_email:
            return False
        if not self.email_ok:
            return False
        self.email_sent.append(author_email)
        return True


@pytest.fixture
def bot_store(monkeypatch, tmp_path):
    store = ConversationStore(path=str(tmp_path / "c.json"))
    monkeypatch.setattr(
        "threadweave.connectors.teams.conversations._store", store
    )
    return store


def remember(store, person_id, conversation_type):
    """Store a ref with an explicit conversation type."""
    from types import SimpleNamespace

    activity = SimpleNamespace(
        id="a1",
        conversation=SimpleNamespace(conversation_type=conversation_type),
        from_property=SimpleNamespace(id=person_id, aad_object_id=person_id),
        recipient=SimpleNamespace(id="bot1", name="ThreadWeave"),
    )
    store.remember(
        person_id, "conv-1", "https://smba.example.com", activity=activity
    )


def make_notif(nid, author_id):
    return {
        "id": nid, "entry_id": f"e-{nid}", "author_id": author_id,
        "title": "The pipeline decision", "wing": "engineering",
        "room": "general", "source": "teams",
    }


@pytest.mark.asyncio
async def test_delivery_dms_personal_ref(bot_store):
    remember(bot_store, "aad-123", "personal")
    bot = BotUnderTest([make_notif("n1", "aad-123")])
    await bot._deliver_pending_notifications()
    assert bot.dm_refs == ["personal"]
    assert bot.activity_targets == []
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_channel_ref_never_receives_dm(bot_store):
    remember(bot_store, "aad-123", "channel")
    bot = BotUnderTest([make_notif("n1", "aad-123")])
    await bot._deliver_pending_notifications()
    assert bot.dm_refs == []  # the channel-posting bug must stay fixed
    assert bot.activity_targets == ["aad-123"]
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_passive_author_gets_activity_notification(bot_store):
    # Never talked to the bot: no ref, email resolves to an AAD id.
    bot = BotUnderTest(
        [make_notif("n1", "adele@x.com")], resolved={"adele@x.com": "aad-a"}
    )
    await bot._deliver_pending_notifications()
    assert bot.dm_refs == []
    assert bot.activity_targets == ["aad-a"]
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_email_ref_channel_type_falls_back_to_activity(bot_store):
    # Ref exists under the AAD id but points at a group chat.
    remember(bot_store, "aad-a", "groupChat")
    bot = BotUnderTest(
        [make_notif("n1", "adele@x.com")], resolved={"adele@x.com": "aad-a"}
    )
    await bot._deliver_pending_notifications()
    assert bot.dm_refs == []
    assert bot.activity_targets == ["aad-a"]


@pytest.mark.asyncio
async def test_undeliverable_skipped_after_max_attempts(bot_store):
    bot = BotUnderTest(
        [make_notif("n1", "ghost@x.com")], resolved={}, max_attempts=3
    )
    bot.email_ok = False  # email fallback also unavailable
    for _ in range(3):
        await bot._deliver_pending_notifications()
    # 3 failures -> marked skipped, nothing marked delivered.
    assert bot.delivered_paths == [
        "/api/v1/notifications/n1/delivered?status=skipped"
    ]
    assert bot.stats["notify_skipped"] == 1
    assert bot.stats.get("notified", 0) == 0


@pytest.mark.asyncio
async def test_activity_failure_recovers_before_max(bot_store):
    bot = BotUnderTest(
        [make_notif("n1", "adele@x.com")],
        resolved={"adele@x.com": "aad-a"}, max_attempts=3,
    )
    bot.activity_ok = False
    bot.email_ok = False  # isolate the activity path
    await bot._deliver_pending_notifications()  # attempt 1 fails
    await bot._deliver_pending_notifications()  # attempt 2 fails
    assert bot.delivered_paths == []
    bot.activity_ok = True
    await bot._deliver_pending_notifications()  # attempt 3 succeeds
    assert bot.activity_targets == ["aad-a"]
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_email_fallback_when_activity_fails(bot_store):
    bot = BotUnderTest(
        [make_notif("n1", "adele@x.com")], resolved={"adele@x.com": "aad-a"}
    )
    bot.activity_ok = False  # tenant refuses TeamsActivity.Send
    await bot._deliver_pending_notifications()
    assert bot.activity_targets == []
    assert bot.email_sent == ["adele@x.com"]
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_email_fallback_resolves_aad_id_to_mail(bot_store):
    # teams-watch authors are stored by AAD id, not email.
    bot = BotUnderTest([make_notif("n1", "aad-9")])
    bot.activity_ok = False
    bot.email_map = {"aad-9": "jane@x.com"}
    await bot._deliver_pending_notifications()
    assert bot.email_sent == ["jane@x.com"]
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_email_not_attempted_when_activity_succeeds(bot_store):
    bot = BotUnderTest(
        [make_notif("n1", "adele@x.com")], resolved={"adele@x.com": "aad-a"}
    )
    await bot._deliver_pending_notifications()
    assert bot.activity_targets == ["aad-a"]
    assert bot.email_sent == []
    assert bot.delivered_paths == ["/api/v1/notifications/n1/delivered"]


@pytest.mark.asyncio
async def test_email_disabled_env_skips_fallback(bot_store, monkeypatch):
    monkeypatch.setenv("THREADWEAVE_NOTIFY_EMAIL", "0")
    bot = BotUnderTest([make_notif("n1", "adele@x.com")])
    bot.activity_ok = False
    bot._notify_email_enabled = False  # what the env would set
    await bot._deliver_pending_notifications()
    assert bot.email_sent == []
    assert bot.delivered_paths == []


def test_real_email_sender_guard_requires_sender():
    """The real _send_email_notification refuses without a sender."""
    bot = ThreadWeaveTeamsBot(adapter=None)
    bot._notify_sender = ""
    bot._graph_client = None

    import asyncio

    async def run():
        return await bot._send_email_notification(
            make_notif("n1", "adele@x.com"), "adele@x.com"
        )

    assert asyncio.run(run()) is False  # sender missing, never reaches Graph


def test_ref_is_personal_guard():
    from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot

    assert ThreadWeaveTeamsBot._ref_is_personal(
        {"conversation_type": "personal"})
    assert ThreadWeaveTeamsBot._ref_is_personal({})  # legacy refs
    assert not ThreadWeaveTeamsBot._ref_is_personal(
        {"conversation_type": "channel"})
    assert not ThreadWeaveTeamsBot._ref_is_personal(
        {"conversation_type": "groupChat"})
