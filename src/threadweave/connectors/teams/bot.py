# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
ThreadWeave Teams Bot - passive listener + explicit save flow.

Architecture:
    Teams message -> Detection Engine -> [worth saving?]
        +-- NO  -> silently ignore
        +-- YES -> Adaptive Card to author: "Save this knowledge?"

Modes: passive, explicit, both (default)
The bot NEVER stores anything without explicit user consent.

Dependencies: pip install botbuilder-core botbuilder-integration-aiohttp
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

try:
    from botbuilder.core import (
        ActivityHandler, CardFactory, MessageFactory, TurnContext,
    )
    from botbuilder.schema import (
        Activity, ActivityTypes, Attachment, ChannelAccount,
    )
    BOTBUILDER_AVAILABLE = True
except ImportError:
    BOTBUILDER_AVAILABLE = False

from threadweave.detector import is_worth_saving, DetectionResult

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.25
MAX_TEXT_LENGTH = 8000

EXPLICIT_TRIGGERS = [
    "remember this", "save this", "threadweave save",
    "store this", "save for later", "add to memory",
    "log this", "capture this",
]

SAVE_PROMPT_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "body": [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "text": "Save this knowledge?",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "{snippet}",
            "wrap": True,
            "isSubtle": True,
            "maxLines": 5,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Type", "value": "{content_type}"},
                {"title": "Confidence", "value": "{confidence}"},
                {"title": "Scope", "value": "{scope}"},
            ],
        },
    ],
    "actions": [
        {
            "type": "Action.Submit",
            "title": "Save",
            "data": {"action": "save", "entry_id": "{entry_id}"},
            "style": "positive",
        },
        {
            "type": "Action.Submit",
            "title": "Edit and Save",
            "data": {"action": "edit", "entry_id": "{entry_id}"},
        },
        {
            "type": "Action.Submit",
            "title": "Ignore",
            "data": {"action": "ignore", "entry_id": "{entry_id}"},
        },
    ],
}


class ThreadWeaveTeamsBot(ActivityHandler if BOTBUILDER_AVAILABLE else object):
    """
    Microsoft Teams bot for organizational knowledge detection.

    Passively listens to channel messages and prompts authors to
    save valuable knowledge when the detection engine identifies
    answers, decisions, or structured explanations worth preserving.

    The bot NEVER stores anything without explicit user consent.
    """

    def __init__(
        self,
        api_base_url: str = "http://localhost:8000",
        mode: str = "both",
        min_confidence: float = MIN_CONFIDENCE,
    ):
        if not BOTBUILDER_AVAILABLE:
            raise ImportError(
                "Microsoft Bot Framework SDK not installed. "
                "Run: pip install botbuilder-core botbuilder-integration-aiohttp"
            )
        super().__init__()
        self.api_base_url = api_base_url.rstrip("/")
        self.mode = mode
        self.min_confidence = min_confidence
        self._pending: dict[str, tuple[DetectionResult, str]] = {}  # (result, original_text)
        self.stats = {"detected": 0, "saved": 0, "ignored": 0, "prompted": 0}

    # ---- Lifecycle ----

    async def on_members_added_activity(
        self, members_added: list[ChannelAccount], turn_context: TurnContext
    ):
        """Send welcome message when bot is added to a team."""
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                welcome = (
                    "**ThreadWeave is here!** I help capture organizational "
                    "knowledge from your conversations. I listen passively "
                    "and prompt when something looks worth saving. "
                    "Nothing leaves this team without your approval.\n\n"
                    "- Reply to a message: **@ThreadWeave save this**\n"
                    "- Search: **@ThreadWeave search <query>**\n\n"
                    "_On-premises. No data leaves your Microsoft 365 tenant._"
                )
                await turn_context.send_activity(welcome)
                break

    # ---- Message Handling ----

    async def on_message_activity(self, turn_context: TurnContext):
        activity = turn_context.activity

        if activity.type != ActivityTypes.message:
            return

        # Handle Adaptive Card submit actions
        if activity.value and isinstance(activity.value, dict):
            if "action" in activity.value:
                await self._on_adaptiveresult(turn_context, activity.value)
                return

        text = (activity.text or "").strip()
        from_id = getattr(activity.from_property, "id", None) if activity.from_property else None
        if not text or not from_id or from_id == activity.recipient.id:
            return

        is_mentioned = self._is_bot_mentioned(activity)
        is_explicit = is_mentioned and any(
            trigger in text.lower() for trigger in EXPLICIT_TRIGGERS
        )

        if is_explicit:
            clean = self._strip_trigger_phrases(text)
            if clean:
                await self._handle_explicit(turn_context, activity, clean)
            else:
                await turn_context.send_activity(
                    "What should I remember? Reply with the knowledge."
                )
            return

        if self.mode in ("passive", "both"):
            await self._handle_passive(turn_context, text, activity)

    # ---- Passive Detection ----

    async def _handle_passive(
        self, turn_context: TurnContext, text: str, activity: Activity
    ):
        """Run detection engine on message; prompt if confidence met."""
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH]

        should_save, result = await asyncio.to_thread(
            is_worth_saving, text
        )
        self.stats["detected"] += 1
        logger.info(
            "Passive: type=%s conf=%.2f should_save=%s len=%d text=%r",
            result.content_type.value, result.confidence, should_save,
            len(text), text[:80],
        )

        if not should_save or result.confidence < self.min_confidence:
            return

        self.stats["prompted"] += 1
        fallback = activity.id or "msg"
        entry_id = f"{fallback}_{activity.from_property.id[-8:]}"
        self._pending[entry_id] = (result, text)

        card = self._build_prompt_card(entry_id, result, text)
        await turn_context.send_activity(MessageFactory.attachment(card))

    # ---- Explicit Save ----

    async def _handle_explicit(
        self, turn_context: TurnContext, activity: Activity, text: str
    ):
        """Handle explicit save request (@ThreadWeave save this)."""
        should_save, result = await asyncio.to_thread(
            is_worth_saving, text
        )
        logger.info(
            "Explicit: type=%s conf=%.2f should_save=%s len=%d text=%r",
            result.content_type.value, result.confidence, should_save,
            len(text), text[:80],
        )
        fallback = activity.id or "msg"
        entry_id = f"explicit_{fallback}"

        if result.confidence < 0.1:
            result.confidence = 0.5

        self._pending[entry_id] = (result, text)
        card = self._build_prompt_card(entry_id, result, text)
        await turn_context.send_activity(MessageFactory.attachment(card))

    # ---- Adaptive Card Builder ----

    def _build_prompt_card(
        self, entry_id: str, result: DetectionResult, text: str
    ) -> Attachment:
        """Build the Adaptive Card for save/ignore prompt."""
        snippet = text[:300].replace("\n", " ").replace("\"", "\\\"")

        card_str = json.dumps(SAVE_PROMPT_CARD)
        card_str = (
            card_str.replace("{snippet}", snippet)
            .replace("{content_type}", result.content_type.value)
            .replace("{confidence}", f"{result.confidence:.0%}")
            .replace("{scope}", result.suggested_scope)
            .replace("{entry_id}", entry_id)
        )

        return CardFactory.adaptive_card(json.loads(card_str))

    # ---- Submit Handler (Adaptive Card Actions) ----

    async def _on_adaptiveresult(
        self, turn_context: TurnContext, result: dict
    ):
        """Handle Adaptive Card submit actions (Save / Edit / Ignore)."""
        action = result.get("action", "ignore")
        entry_id = result.get("entry_id", "")

        if action == "save":
            await self._handle_save(turn_context, entry_id)
        elif action == "edit":
            await self._handle_edit(turn_context, entry_id)
        else:
            await self._handle_ignore(turn_context, entry_id)

    async def _handle_save(self, turn_context: TurnContext, entry_id: str):
        """Save the pending detection to ThreadWeave API."""
        stored = self._pending.pop(entry_id, None)
        if not stored:
            await turn_context.send_activity(
                "Could not find that entry. It may have expired."
            )
            return

        detection, original_text = stored

        try:
            saved = await self._save_to_api(
                content=original_text,
                content_type=detection.content_type.value,
                scope=detection.suggested_scope,
                title=detection.suggested_title,
                confidence=detection.confidence,
            )

            self.stats["saved"] += 1
            title = saved.get("suggested_title", "Knowledge")
            ctype = saved.get("content_type", "unknown")
            await turn_context.send_activity(
                f"Saved! '{title}' [{ctype}] ID: {saved.get('id', '?')}"
            )
        except Exception as e:
            logger.error("Failed to save entry: %s", e)
            await turn_context.send_activity(
                f"Failed to save: {e}. The knowledge is not lost — "
                "please try again or save manually."
            )

    async def _handle_edit(self, turn_context: TurnContext, entry_id: str):
        """Prompt user to edit before saving (opens a text input)."""
        stored = self._pending.get(entry_id)
        if not stored:
            await turn_context.send_activity("Entry not found.")
            return

        detection, _ = stored
        preview = detection.suggested_title or "this knowledge"
        await turn_context.send_activity(
            f"**Edit mode:** Reply with the final version of '{preview}' "
            f"and I'll save it. Or just say 'save as is' to use the original."
        )
        # Store that this entry is in edit mode
        self._pending[f"editing_{entry_id}"] = stored

    async def _handle_ignore(self, turn_context: TurnContext, entry_id: str):
        """User chose not to save — quietly discard."""
        self._pending.pop(entry_id, None)
        self.stats["ignored"] += 1
        # Silent — no message needed

    # ---- ThreadWeave API Client (central ingestion) ----

    async def _save_to_api(
        self,
        content: str,
        content_type: str = "answer",
        scope: str = "team",
        title: str = "",
        confidence: float = 0.5,
    ) -> dict:
        """Save knowledge via central ingestion pipeline POST /api/v1/ingest."""
        import httpx

        payload = {
            "content": content,
            "source": "teams",
            "tenant_id": "default",
            "metadata": {
                "wing": "general",
                "room": content_type,
                "title": title,
                "scope": scope,
                "content_type": content_type,
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.api_base_url}/api/v1/ingest",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()

        # The ingest pipeline gates on the detector (should_save >= 0.40).
        # When the user EXPLICITLY approved the save via the card, their
        # consent overrides the heuristic — fall back to the unconditional
        # save endpoint instead of reporting a fake success. (Fixed
        # 2026-08-05: explicit saves of low-confidence content previously
        # returned id="not_saved" and the bot announced "Saved!" anyway.)
        if result.get("id") == "not_saved" or result.get("should_save") is False:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.api_base_url}/api/v1/entries",
                    json={
                        "content": content,
                        "wing": "general",
                        "room": content_type,
                        "scope": scope,
                        "source_type": "teams",
                        "title": title,
                        "tenant_id": "default",
                    },
                )
                resp.raise_for_status()
                return resp.json()

        return result

    # ---- Helpers ----


    def _is_bot_mentioned(self, activity: Activity) -> bool:
        """Check if bot is @mentioned in the message."""
        if activity.entities:
            for entity in activity.entities:
                if entity.type == "mention":
                    # Modern botbuilder (>=4.15) exposes mention data via
                    # additional_properties (Entity is a msrest Model now,
                    # not a dict — .get() raises AttributeError). Fall back
                    # to a plain dict for older SDK versions.
                    mentioned = {}
                    if hasattr(entity, "additional_properties"):
                        mentioned = entity.additional_properties.get("mentioned", {})
                    elif isinstance(entity, dict):
                        mentioned = entity.get("mentioned", {})
                    if mentioned.get("id") == activity.recipient.id:
                        return True

        bot_name = activity.recipient.name or ""
        text = (activity.text or "").lower()
        return bool(bot_name and bot_name.lower() in text)

    def _strip_trigger_phrases(self, text: str) -> str:
        """Remove explicit save triggers from text."""
        text_lower = text.lower()
        for trigger in EXPLICIT_TRIGGERS:
            if trigger in text_lower:
                idx = text_lower.index(trigger)
                return text[idx + len(trigger):].strip().lstrip(":.")

        return re.sub(r"<at>.*?</at>", "", text).strip()