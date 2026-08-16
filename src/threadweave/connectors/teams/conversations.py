# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Conversation registry — enables the bot's capture notifications.

To DM a person proactively ("your email about X was added to the
palace"), the bot needs their 1:1 conversation reference. The bot
captures references whenever someone talks to it (DM or @mention)
and persists them here.

Storage: ~/.threadweave/bot_conversations.json (person id -> ref).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = "~/.threadweave/bot_conversations.json"


class ConversationStore:
    """Persist conversation references by person identity."""

    def __init__(self, path: str | None = None):
        self.path = os.path.expanduser(
            path or os.environ.get(
                "THREADWEAVE_CONVERSATIONS_FILE", DEFAULT_STORE_PATH
            )
        )
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Conversation store load failed: %s", exc)
            return {}

    def _save(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            logger.warning("Conversation store save failed: %s", exc)

    def remember(
        self,
        person_id: str,
        conversation_id: str,
        service_url: str,
        channel_id: str = "msteams",
        name: str = "",
        activity: object | None = None,
    ) -> None:
        """Record/refresh a person's conversation reference.

        When an activity is provided, the full Bot Framework
        ConversationReference is derived from it (the modern adapter
        needs the complete reference for proactive messages).
        """
        if not person_id or not conversation_id:
            return
        ref = {
            "conversation_id": conversation_id,
            "service_url": service_url,
            "channel_id": channel_id,
            "name": name,
        }
        if activity is not None:
            try:
                act = activity
                ref["activity_id"] = getattr(act, "id", "") or ""
                conv = act.conversation
                if conv is not None:
                    ref["conversation_type"] = getattr(conv, "conversation_type", "") or ""
                user = act.from_property
                if user is not None:
                    ref["user_id"] = getattr(user, "id", "") or ""
                    ref["user_aad_id"] = getattr(user, "aad_object_id", "") or ""
                bot = act.recipient
                if bot is not None:
                    ref["bot_id"] = getattr(bot, "id", "") or ""
                    ref["bot_name"] = getattr(bot, "name", "") or ""
            except Exception:
                pass  # best-effort reference capture
        with self._lock:
            existing = self._data.get(person_id)
            if existing:
                existing_type = existing.get("conversation_type", "")
                new_type = ref.get("conversation_type", "")
                if existing_type == "personal" and new_type == "channel":
                    # Org-wide RSC delivers every channel message, so
                    # channel activities would constantly overwrite a
                    # person's 1:1 ref and silently disable the DM
                    # camera-sign leg (live 2026-08-16). A personal
                    # ref is strictly better: keep it.
                    return
            self._data[person_id] = ref
            self._save()

    def get(self, person_id: str) -> dict | None:
        return self._data.get(person_id)

    def known(self) -> list[str]:
        return list(self._data.keys())


# Process-wide singleton
_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
