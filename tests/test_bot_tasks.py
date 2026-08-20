# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Teams bot action-item (task) command tests."""

import asyncio

from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot


class FakeTurnContext:
    def __init__(self):
        self.sent = []

    async def send_activity(self, text):
        self.sent.append(text)


class TaskBot(ThreadWeaveTeamsBot):
    """Bot with a stubbed API returning scripted task payloads."""

    def __init__(self, scripted=None, mode="both"):
        super().__init__(adapter=None)
        self.mode = mode
        self.scripted = scripted or {}
        self.posts = []

    async def _api_get(self, path):
        return self.scripted.get("get", {}).get(path, {"tasks": []})

    async def _api_post(self, path, body):
        self.posts.append((path, body))
        return {"ok": True}


class FakeActivity:
    def __init__(self, aad_id="caller-123"):
        self.from_property = type("FP", (), {"aad_object_id": aad_id})()


TASKS_PAYLOAD = {
    "tasks": [
        {"id": "t1", "action": "chase the vendor",
         "deadline": "2026-08-21", "owner": "caller-123",
         "owner_name": "Caller", "status": "open"},
        {"id": "t2", "action": "write the report",
         "deadline": "", "owner": "caller-123",
         "owner_name": "Caller", "status": "open"},
    ]
}


def _run(coro):
    return asyncio.run(coro)


def test_my_tasks_lists_owner():
    bot = TaskBot(scripted={"get": {
        "/api/v1/tasks?owner=caller-123": TASKS_PAYLOAD,
    }})
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "my tasks"))
    assert handled is True
    assert "2 open action item(s)" in ctx.sent[0]
    assert "chase the vendor" in ctx.sent[0]
    assert "[by 2026-08-21]" in ctx.sent[0]


def test_tasks_for_name():
    bot = TaskBot(scripted={"get": {
        "/api/v1/tasks?owner=Adele": {"tasks": [
            {"id": "t9", "action": "look into azure", "deadline": "",
             "owner": "adele", "owner_name": "Adele", "status": "open"},
        ]},
    }})
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "tasks for Adele"))
    assert handled is True
    assert "1 open action item(s) for Adele" in ctx.sent[0]
    assert "look into azure" in ctx.sent[0]


def test_my_tasks_empty():
    bot = TaskBot()  # no scripted get → returns empty
    ctx = FakeTurnContext()
    _run(bot._handle_tasks_command(ctx, FakeActivity(), "my tasks"))
    assert ctx.sent[0] == "No open action items for 'caller-123'."


def test_tasks_done_marks_nth():
    bot = TaskBot(scripted={"get": {
        "/api/v1/tasks?owner=caller-123": TASKS_PAYLOAD,
    }})
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "tasks done 2"))
    assert handled is True
    # nth=2 → t2 → POST /api/v1/tasks/t2/done
    assert any(p == ("/api/v1/tasks/t2/done", {}) for p in bot.posts)
    assert "write the report" in ctx.sent[0]


def test_tasks_done_without_index_prompts():
    bot = TaskBot()
    ctx = FakeTurnContext()
    _run(bot._handle_tasks_command(ctx, FakeActivity(), "tasks done"))
    assert "Which one?" in ctx.sent[0]


def test_tasks_not_done_reopens():
    bot = TaskBot(scripted={"get": {
        "/api/v1/tasks?owner=caller-123": TASKS_PAYLOAD,
    }})
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "tasks not done 1"))
    assert handled is True
    assert any(p == ("/api/v1/tasks/t1/undone", {}) for p in bot.posts)


def test_tasks_search():
    bot = TaskBot(scripted={"get": {
        "/api/v1/tasks?status=all": TASKS_PAYLOAD,
    }})
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "tasks search vendor"))
    assert handled is True
    # only t1 matches "vendor"
    assert "1 action item(s) matching 'vendor'" in ctx.sent[0]
    assert "chase the vendor" in ctx.sent[0]


def test_unrelated_text_not_handled():
    bot = TaskBot()
    ctx = FakeTurnContext()
    handled = _run(bot._handle_tasks_command(ctx, FakeActivity(), "just chatting"))
    assert handled is False
    assert ctx.sent == []
