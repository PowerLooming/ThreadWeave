# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Google Chat Listener — captures knowledge from Google Chat spaces.

Polls Google Chat spaces for recent messages, detects knowledge-worthy
content, and submits to ThreadWeave ingestion pipeline.

Uses Google Chat API v1: spaces.messages.list

Design:
    - Poll-based — Chat API doesn't support push notifications
    - Tracks last processed timestamp per space
    - Skips bot messages and short/chatty content
    - Groups messages by thread for context
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from threadweave.connectors.gws.auth import GWSAuth

logger = logging.getLogger("threadweave.gws.chat")


@dataclass
class ChatMessage:
    """A parsed Google Chat message."""
    message_id: str
    space_id: str
    space_name: str
    sender: str           # Display name or email
    sender_type: str      # HUMAN or BOT
    text: str
    thread_id: str = ""
    timestamp: str = ""   # ISO 8601
    is_thread_reply: bool = False


class ChatListener:
    """Listen to Google Chat spaces for knowledge.

    Polls the Chat API for recent messages in configured spaces.
    """

    MAX_RESULTS = 50

    def __init__(
        self,
        auth: GWSAuth,
        threadweave_url: str = "http://localhost:8000",
    ):
        self.auth = auth
        self.threadweave_url = threadweave_url.rstrip("/")
        self._last_poll: dict[str, str] = {}  # space_id → last timestamp

    # ── Fetch ─────────────────────────────────────────────────────

    def list_spaces(self) -> list[dict]:
        """List all Google Chat spaces the service account can access."""
        service = self.auth.chat()
        spaces = []
        page_token = None

        try:
            while True:
                params = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token

                result = service.spaces().list(**params).execute()
                spaces.extend(result.get("spaces", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            logger.error("Failed to list Chat spaces: %s", exc)

        return spaces

    def fetch_messages(
        self, space_id: str, max_results: int = MAX_RESULTS,
    ) -> list[ChatMessage]:
        """Fetch recent messages from a specific Chat space.

        Args:
            space_id: Google Chat space ID (e.g., 'spaces/AAA123').
            max_results: Maximum number of messages.

        Returns:
            List of parsed ChatMessage objects.
        """
        service = self.auth.chat()

        try:
            result = (
                service.spaces()
                .messages()
                .list(parent=space_id, pageSize=max_results)
                .execute()
            )
        except Exception as exc:
            logger.warning("Failed to fetch Chat messages from %s: %s", space_id, exc)
            return []

        messages = result.get("messages", [])
        parsed = []

        for msg in messages:
            parsed_msg = self._parse_message(msg, space_id)
            if parsed_msg and self._is_new(parsed_msg):
                parsed.append(parsed_msg)

        if parsed:
            self._last_poll[space_id] = max(
                p.timestamp for p in parsed if p.timestamp
            ) or ""

        return parsed

    def _parse_message(self, raw: dict, space_id: str) -> Optional[ChatMessage]:
        """Parse a raw Chat API message."""
        name = raw.get("name", "")  # "spaces/{space}/messages/{msg}"
        msg_id = name.split("/")[-1] if "/" in name else name

        sender_info = raw.get("sender", {})
        sender = sender_info.get("displayName", sender_info.get("email", "unknown"))
        sender_type = sender_info.get("type", "HUMAN")

        # Skip bot messages
        if sender_type == "BOT":
            return None

        # Extract text
        text = raw.get("text", "").strip()
        if not text or len(text) < 20:
            return None

        # Thread info
        thread_info = raw.get("thread", {})
        thread_id = ""
        is_reply = False
        if thread_info:
            thread_name = thread_info.get("name", "")
            thread_id = thread_name.split("/")[-1] if "/" in thread_name else thread_name
            is_reply = thread_info.get("threadReply", False)

        # Timestamp
        timestamp = raw.get("createTime", "")
        if timestamp:
            # Chat API returns RFC 3339, normalize to ISO 8601
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                timestamp = dt.isoformat()
            except ValueError:
                pass

        # Get space display name
        space_name = space_id.split("/")[-1] if "/" in space_id else space_id

        return ChatMessage(
            message_id=msg_id,
            space_id=space_id,
            space_name=space_name,
            sender=sender,
            sender_type=sender_type,
            text=text,
            thread_id=thread_id,
            timestamp=timestamp,
            is_thread_reply=is_reply,
        )

    def _is_new(self, msg: ChatMessage) -> bool:
        """Check if this message is newer than our last poll."""
        if not msg.timestamp or msg.space_id not in self._last_poll:
            return True
        return msg.timestamp > self._last_poll[msg.space_id]

    # ── Submit ────────────────────────────────────────────────────

    def submit_messages(
        self, messages: list[ChatMessage],
    ) -> dict:
        """Submit Chat messages to ThreadWeave ingestion pipeline."""
        stats = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}

        for msg in messages:
            stats["submitted"] += 1
            # Thread context: prepend reply indicator
            content = msg.text
            if msg.is_thread_reply:
                content = f"[In reply to thread {msg.thread_id}]\n{msg.text}"

            try:
                resp = requests.post(
                    f"{self.threadweave_url}/api/v1/ingest",
                    json={
                        "content": content,
                        "source": "google_chat",
                        "metadata": {
                            "message_id": msg.message_id,
                            "space_id": msg.space_id,
                            "space_name": msg.space_name,
                            "sender": msg.sender,
                            "timestamp": msg.timestamp,
                            "thread_id": msg.thread_id,
                            "wing": msg.space_name,  # Map space → wing
                            "room": "chat",
                        },
                    },
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    if data.get("should_save"):
                        stats["saved"] += 1
                    else:
                        stats["skipped"] += 1
                else:
                    stats["errors"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("Failed to submit Chat message: %s", exc)

        return stats

    def process_all_spaces(self) -> dict:
        """Fetch messages from ALL accessible Chat spaces and submit."""
        stats = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}
        spaces = self.list_spaces()

        for space in spaces:
            space_id = space.get("name", "")
            if not space_id:
                continue
            messages = self.fetch_messages(space_id)
            s = self.submit_messages(messages)
            for k in stats:
                stats[k] += s[k]

        return stats
