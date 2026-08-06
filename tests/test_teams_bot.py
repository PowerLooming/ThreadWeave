# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the Teams bot conversation-context derivation.

The bot maps Teams conversations onto the palace model:
- channel message -> wing = team name, room = channel name
- group chat      -> wing = "general", room = "group-chat"
- DM              -> wing = "general", room = "dm"
"""

import sys
from types import SimpleNamespace

import pytest

botbuilder = pytest.importorskip("botbuilder")

sys.path.insert(0, "src")
from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot


def make_bot():
    return ThreadWeaveTeamsBot(api_base_url="http://localhost:8000")


def make_activity(channel_data: dict):
    return SimpleNamespace(channel_data=channel_data)


def test_channel_message_uses_team_and_channel_names():
    bot = make_bot()
    act = make_activity({
        "chatType": "channel",
        "team": {"id": "t1", "name": "Mark 8 Project Team"},
        "channel": {"id": "c1", "name": "general"},
    })
    ctx = bot._conversation_context(act)
    assert ctx == {"wing": "Mark 8 Project Team", "room": "general"}


def test_channel_message_missing_team_name_falls_back():
    bot = make_bot()
    act = make_activity({
        "chatType": "channel",
        "channel": {"id": "c1", "name": "architecture"},
    })
    ctx = bot._conversation_context(act)
    assert ctx == {"wing": "general", "room": "architecture"}


def test_group_chat_maps_to_general_wing():
    bot = make_bot()
    act = make_activity({"chatType": "groupChat"})
    ctx = bot._conversation_context(act)
    assert ctx == {"wing": "general", "room": "group-chat"}


def test_dm_maps_to_general_wing():
    bot = make_bot()
    act = make_activity({})
    ctx = bot._conversation_context(act)
    assert ctx == {"wing": "general", "room": "dm"}


def test_missing_channel_data_is_safe():
    bot = make_bot()
    act = SimpleNamespace()  # no channel_data attribute at all
    ctx = bot._conversation_context(act)
    assert ctx == {"wing": "general", "room": "dm"}
