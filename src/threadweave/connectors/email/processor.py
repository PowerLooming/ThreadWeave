# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Email Processor — extracts knowledge from email threads.

Handles:
    - HTML-to-text conversion for email bodies
    - Thread reconstruction (reply chains -> single document)
    - Detection engine integration
    - MemPalace mining with email metadata (sender, thread, timestamps)

Pipeline:
    EmailMessage -> extract body -> reconstruct thread ->
    detect worth saving -> [yes] -> mine to MemPalace
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from threadweave.detector import is_worth_saving, DetectionResult
from threadweave.connectors.email.watcher import (
    EmailMessage,
    EmailThread,
)

logger = logging.getLogger(__name__)

# Email threading patterns to strip when reconstructing
REPLY_HEADER_PATTERNS = [
    re.compile(r"^On .+ wrote:\n", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From: .+\nSent: .+\nTo: .+\nSubject: .+\n", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}Original Message-{2,}.*?\n", re.DOTALL | re.IGNORECASE),
    re.compile(r"^>+.*$", re.MULTILINE),
]

EMAIL_MIN_CONFIDENCE = 0.20
MIN_BODY_LENGTH = 100


from dataclasses import dataclass, field

@dataclass
class ProcessedEmail:
    source: str
    conversation_id: str
    subject: str
    participants: list[str]
    text_content: str
    word_count: int
    detection: DetectionResult | None = None
    should_save: bool = False
    drawer_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EmailProcessor:
    """Processes email content for organizational knowledge extraction."""

    def __init__(
        self,
        mempalace_palace_path: str = "~/.mempalace/palace",
        min_confidence: float = EMAIL_MIN_CONFIDENCE,
    ):
        self.palace_path = os.path.expanduser(mempalace_palace_path)
        self.min_confidence = min_confidence
        self.stats = {
            "emails_processed": 0,
            "threads_processed": 0,
            "knowledge_extracted": 0,
            "skipped": 0,
        }

    async def process_message(self, email: EmailMessage) -> ProcessedEmail:
        """Process a single email message."""
        text = self._extract_body(email)
        if len(text) < MIN_BODY_LENGTH:
            self.stats["skipped"] += 1
            return ProcessedEmail(
                source="single",
                conversation_id=email.conversation_id,
                subject=email.subject,
                participants=[email.sender_email] + email.recipients,
                text_content=text,
                word_count=len(text.split()),
            )
        self.stats["emails_processed"] += 1
        return await self._detect_and_save(text=text, source="single", email=email)

    async def process_thread(self, thread: EmailThread) -> ProcessedEmail:
        """Process an entire email thread as one knowledge unit."""
        if not thread.messages:
            return ProcessedEmail(
                source="thread", conversation_id=thread.conversation_id,
                subject=thread.subject, participants=[], text_content="", word_count=0,
            )
        parts = []
        for msg in thread.messages:
            body = self._extract_body(msg)
            if body.strip():
                parts.append(body)
        full_text = "\n\n---\n\n".join(parts)
        if len(full_text) < MIN_BODY_LENGTH:
            self.stats["skipped"] += 1
            return ProcessedEmail(
                source="thread", conversation_id=thread.conversation_id,
                subject=thread.subject,
                participants=self._thread_participants(thread),
                text_content=full_text, word_count=len(full_text.split()),
            )
        self.stats["threads_processed"] += 1
        return await self._detect_and_save(
            text=full_text, source="thread", email=thread.messages[0],
            conversation_id=thread.conversation_id,
            participants=self._thread_participants(thread),
            thread_message_count=len(thread.messages),
        )

    async def _detect_and_save(
        self, text, source, email, conversation_id=None,
        participants=None, thread_message_count=1,
    ) -> ProcessedEmail:
        result = ProcessedEmail(
            source=source,
            conversation_id=conversation_id or email.conversation_id,
            subject=email.subject,
            participants=participants or [email.sender_email],
            text_content=text, word_count=len(text.split()),
        )
        should_save, detection = await asyncio.to_thread(is_worth_saving, text)
        result.detection = detection
        result.should_save = should_save
        if not should_save or detection.confidence < self.min_confidence:
            self.stats["skipped"] += 1
            return result
        try:
            drawer_ids = await self._mine_to_mempalace(
                text=text, subject=email.subject, sender=email.sender_email,
                conversation_id=conversation_id or email.conversation_id,
                participants=participants or [email.sender_email],
                received_at=email.received_at,
                content_type=detection.content_type.value,
                scope=detection.suggested_scope,
                thread_messages=thread_message_count,
            )
            result.drawer_ids = drawer_ids
            self.stats["knowledge_extracted"] += 1
        except Exception as e:
            logger.error("Failed to save to MemPalace: %s", e)
            result.errors.append(str(e))
        return result

    def _extract_body(self, email: EmailMessage) -> str:
        if email.body_text:
            return self._strip_reply_headers(email.body_text)
        if email.body_html:
            return self._html_to_text(email.body_html)
        return ""

    def _html_to_text(self, html: str) -> str:
        try:
            from html.parser import HTMLParser
            class TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []
                    self.skip = False
                def handle_starttag(self, tag, attrs):
                    if tag in ("style", "script", "head"):
                        self.skip = True
                def handle_endtag(self, tag):
                    if tag in ("style", "script", "head"):
                        self.skip = False
                    if tag in ("p", "br", "div", "li", "tr"):
                        self.text.append("\n")
                def handle_data(self, data):
                    if not self.skip:
                        self.text.append(data)
            extractor = TextExtractor()
            extractor.feed(html)
            text = "".join(extractor.text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]+", " ", text)
            return self._strip_reply_headers(text.strip())
        except Exception as e:
            logger.warning("HTML extraction failed: %s", e)
            text = re.sub(r"<[^>]+>", "", html)
            text = re.sub(r"&[a-z]+;", " ", text)
            return self._strip_reply_headers(text.strip())

    def _strip_reply_headers(self, text: str) -> str:
        for pattern in REPLY_HEADER_PATTERNS:
            text = pattern.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _mine_to_mempalace(
        self, text, subject, sender, conversation_id, participants,
        received_at, content_type="answer", scope="team", thread_messages=1,
    ) -> list[str]:
        """Submit email knowledge to the central ingestion pipeline."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "http://localhost:8000/api/v1/ingest",
                    json={
                        "content": text,
                        "source": "email",
                        "tenant_id": "default",
                        "metadata": {
                            "wing": "email",
                            "room": content_type,
                            "title": subject,
                            "email_sender": sender,
                            "email_conversation_id": conversation_id,
                            "email_participants": ",".join(participants[:20]),
                            "email_received_at": received_at,
                            "email_thread_messages": str(thread_messages),
                            "scope": scope,
                            "content_type": content_type,
                        },
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return [data.get("id", "")] if data.get("should_save") else []
        except Exception as e:
            logger.error("Ingest API call failed: %s", e)
            raise

    @staticmethod
    def _thread_participants(thread: EmailThread) -> list[str]:
        participants = set()
        for msg in thread.messages:
            participants.add(msg.sender_email)
            participants.update(msg.recipients)
            participants.update(msg.cc_recipients)
        return sorted(participants)

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 4000) -> list[str]:
        if len(text) <= max_chars:
            return [text] if text.strip() else []
        chunks = []
        sentences = re.split(r"(?<=[.!?])\s+", text)
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}" if current else sentence
            if len(candidate) > max_chars and current:
                chunks.append(current.strip())
                current = sentence
            else:
                current = candidate
        if current.strip():
            chunks.append(current.strip())
        return chunks
