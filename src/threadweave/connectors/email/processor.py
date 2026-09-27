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

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from threadweave.detector import is_worth_saving_async, DetectionResult
from threadweave.auth import api_headers
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

# NDR / bounce detection. Undeliverable notifications are system noise,
# not organizational knowledge: the bot's notification emails bounce
# back as "Undeliverable:" NDRs when the recipient has no mailbox or the
# app isn't installed. They also embed the original message's raw
# headers, which trips the conservative PII patterns (Exchange
# anti-spam ARA tokens like 23010399003 match the Nordic personal-ID
# shape 6+5 digits) and rejects the whole thread. Skip them early.
BOUNCE_SUBJECT_PATTERNS = [
    re.compile(r"^undeliverable:", re.IGNORECASE),
    re.compile(r"^delivery (?:status )?notification", re.IGNORECASE),
    re.compile(r"^mail delivery (?:failed|failure)", re.IGNORECASE),
    re.compile(r"^delivery has failed", re.IGNORECASE),
    re.compile(r"^returned mail", re.IGNORECASE),
    re.compile(r"^failure notice", re.IGNORECASE),
    re.compile(r"^non[- ]?deliverable", re.IGNORECASE),
    re.compile(r"^message delivery failure", re.IGNORECASE),
]
BOUNCE_BODY_MARKERS = [
    "diagnostic-code",
    "final-recipient",
    "x-failed-recipients",
    "delivery has failed",
    "could not be delivered",
    "your message wasn't delivered",
]

EMAIL_MIN_CONFIDENCE = 0.40   # save threshold (ANSWER/DECISION confidence >= this)
MIN_BODY_LENGTH = 100          # skip bodies shorter than this (chars)

# Env overrides so the save threshold and body-length floor are tunable
# without a code edit (pilot calibration).
_ENV_MIN_CONFIDENCE = "THREADWEAVE_EMAIL_MIN_CONFIDENCE"
_ENV_MIN_BODY_LENGTH = "THREADWEAVE_EMAIL_MIN_BODY_LENGTH"
_ENV_API_URL = "THREADWEAVE_API_URL"
_ENV_INGEST_TIMEOUT = "THREADWEAVE_INGEST_TIMEOUT"
_ENV_NOISE_FILTER = "THREADWEAVE_EMAIL_NOISE_FILTER"

# System mail that is never organizational knowledge: password-expiry notices,
# vendor security digests, ticketing notifications. A local LLM used to read
# every one of them in full before detection discarded them, which is how a
# five-message cycle took twenty minutes. The patterns are deliberately narrow:
# a person who signs a message is never skipped by the sender rule, and the
# subject rule needs a cadence word (weekly digest) or the word newsletter,
# not the word digest on its own.
_AUTOMATED_LOCAL = re.compile(
    r"(?:^|[-_.])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|"
    r"mailer[-_.]?daemon|postmaster|bounce[sd]?|notifications?|alerts?)"
    r"(?:$|[-_.])",
    re.IGNORECASE,
)
_REPLY_PREFIX = re.compile(r"^(?:re|fw|fwd|aw|sv|vs)\s*:", re.IGNORECASE)
_DIGEST_SUBJECT = re.compile(
    r"\b(?:weekly|monthly|daily|quarterly)\b[^.]{0,40}?"
    r"\b(?:digest|report|roundup|bulletin)\b"
    r"|\bnewsletter\b"
    r"|\bdigest\s+for\b",
    re.IGNORECASE,
)
_ENV_TENANT = "THREADWEAVE_TENANT"


def _api_url() -> str:
    """The ThreadWeave API to submit to (the other daemons honour the same var)."""
    return os.environ.get(_ENV_API_URL, "http://localhost:8000").rstrip("/")


def _ingest_timeout() -> float:
    """Seconds to wait for a capture to be accepted.

    The server runs detection (possibly a local LLM) and the MemPalace write
    inside the request, so a 30s ceiling timed out on healthy captures: the
    entry was stored while the watcher reported it as failed.
    """
    try:
        return float(os.environ.get(_ENV_INGEST_TIMEOUT, "180"))
    except ValueError:
        return 180.0


def _tenant() -> str:
    """Which tenant a captured mail belongs to.

    The managed tier files under "default". A personal deployment is read as
    the "personal" tenant, and a search that names a tenant excludes every
    other one, so filing personal captures under "default" would hide the
    owner's own mail from their own searches.
    """
    explicit = os.environ.get(_ENV_TENANT)
    if explicit:
        return explicit
    from threadweave.profile import is_personal

    return "personal" if is_personal() else "default"


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
    # Why a message was not even offered to the detector. "noise" marks system
    # mail the filter recognised, which the daemons report separately so an
    # operator can tell an empty mailbox from a filtered one.
    skipped_reason: str = ""
    noise: bool = False


class EmailProcessor:
    """Processes email content for organizational knowledge extraction."""

    def __init__(
        self,
        mempalace_palace_path: str = "~/.mempalace/palace",
        min_confidence: float | None = None,
        min_body_length: int | None = None,
        graph_client=None,
    ):
        self.palace_path = os.path.expanduser(mempalace_palace_path)
        self.min_confidence = (
            min_confidence if min_confidence is not None
            else float(os.environ.get(_ENV_MIN_CONFIDENCE, EMAIL_MIN_CONFIDENCE))
        )
        self.min_body_length = (
            min_body_length if min_body_length is not None
            else int(os.environ.get(_ENV_MIN_BODY_LENGTH, MIN_BODY_LENGTH))
        )
        # Optional Graph client for sender -> department -> wing mapping.
        # Without it, all email lands in the "email" wing (fallback).
        self.graph = graph_client
        self._wing_cache: dict[str, str] = {}
        # On by default: skipping automated mail before the detector is both
        # faster and free of judgment, and an operator who wants the old
        # behaviour can set THREADWEAVE_EMAIL_NOISE_FILTER=0.
        self.noise_filter_enabled = os.environ.get(
            _ENV_NOISE_FILTER, "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.stats = {
            "emails_processed": 0,
            "threads_processed": 0,
            "knowledge_extracted": 0,
            "skipped": 0,
            "noise_skipped": 0,
        }

    def _is_system_noise(self, email: EmailMessage) -> str:
        """Why this message is system mail, or "" if it is worth detecting."""
        if not self.noise_filter_enabled:
            return ""
        sender = (email.sender_email or "").strip().lower()
        local = sender.split("@", 1)[0] if "@" in sender else sender
        if _AUTOMATED_LOCAL.search(local):
            return f"automated sender {sender}"
        subject = email.subject or ""
        # A reply or a forward means a person is in the conversation: someone
        # answered the digest or passed it on, which is exactly the kind of
        # message the recollection is for. Only the arriving copy is skipped.
        if not _REPLY_PREFIX.match(subject) and _DIGEST_SUBJECT.search(subject):
            return "recurring digest subject"
        return ""

    async def process_message(self, email: EmailMessage) -> ProcessedEmail:
        """Process a single email message."""
        if self._is_bounce(email):
            self.stats["skipped"] += 1
            return ProcessedEmail(
                source="single",
                conversation_id=email.conversation_id,
                subject=email.subject,
                participants=[email.sender_email] + email.recipients,
                text_content="",
                word_count=0,
            )
        noise = self._is_system_noise(email)
        if noise:
            self.stats["skipped"] += 1
            self.stats["noise_skipped"] += 1
            logger.info("Skipped system mail (%s): %s", noise,
                        (email.subject or "")[:70])
            return ProcessedEmail(
                source="single",
                conversation_id=email.conversation_id,
                subject=email.subject,
                participants=[email.sender_email] + email.recipients,
                text_content="",
                word_count=0,
                skipped_reason=noise,
                noise=True,
            )
        text = self._extract_body(email)
        if len(text) < self.min_body_length:
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
        return await self._detect_and_save(
            text=text, source="single", email=email,
            participants=[email.sender_email] + email.recipients,
        )

    async def process_thread(self, thread: EmailThread) -> ProcessedEmail:
        """Process an entire email thread as one knowledge unit."""
        if not thread.messages:
            return ProcessedEmail(
                source="thread", conversation_id=thread.conversation_id,
                subject=thread.subject, participants=[], text_content="", word_count=0,
            )
        # A thread of nothing but digests is a digest, not a conversation.
        if all(self._is_system_noise(m) for m in thread.messages):
            self.stats["skipped"] += 1
            self.stats["noise_skipped"] += 1
            logger.info("Skipped system thread: %s", (thread.subject or "")[:70])
            return ProcessedEmail(
                source="thread", conversation_id=thread.conversation_id,
                subject=thread.subject,
                participants=self._thread_participants(thread),
                text_content="", word_count=0,
                skipped_reason="all messages are system mail",
                noise=True,
            )
        # An NDR in the thread means the whole exchange bounced; skip it
        # rather than extracting quoted headers that trip PII patterns.
        if any(self._is_bounce(m) for m in thread.messages):
            self.stats["skipped"] += 1
            return ProcessedEmail(
                source="thread", conversation_id=thread.conversation_id,
                subject=thread.subject,
                participants=self._thread_participants(thread),
                text_content="", word_count=0,
            )
        parts = []
        for msg in thread.messages:
            body = self._extract_body(msg)
            if body.strip():
                parts.append(body)
        full_text = "\n\n---\n\n".join(parts)
        if len(full_text) < self.min_body_length:
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
        should_save, detection = await is_worth_saving_async(
            text, self.min_confidence
        )
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
                web_link=email.web_link,
            )
            result.drawer_ids = drawer_ids
            self.stats["knowledge_extracted"] += 1
        except Exception as e:
            # The capture went to the API, not to MemPalace directly; name the
            # failure and keep the exception type, because httpx timeouts
            # stringify to "" and an empty error tells nobody anything.
            logger.error("Failed to save capture (%s): %s",
                         type(e).__name__, e or "<no message>")
            result.errors.append(f"{type(e).__name__}: {e or '<no message>'}")
        return result

    def _is_bounce(self, email: EmailMessage) -> bool:
        """True for undeliverable/NDR notifications (system noise).

        Checks the subject first (cheap); falls back to body markers for
        bounces with a generic subject. NDR bodies embed the original
        message's raw headers, whose Exchange anti-spam tokens (e.g.
        23010399003) look like Nordic personal IDs and trip the PII gate.
        """
        subject = (getattr(email, "subject", "") or "").strip()
        if any(p.search(subject) for p in BOUNCE_SUBJECT_PATTERNS):
            return True
        body = self._extract_body(email)
        lowered = body[:4000].lower()
        return any(marker in lowered for marker in BOUNCE_BODY_MARKERS)

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
        web_link: str = "",
    ) -> list[str]:
        """Submit email knowledge to the central ingestion pipeline."""
        import httpx

        wing = await self._resolve_wing(sender, recipients=participants)

        metadata = {
            "wing": wing,
            "room": content_type,
            "title": subject,
            "email_sender": sender,
            "email_conversation_id": conversation_id,
            "email_participants": ",".join(participants[:20]),
            "email_received_at": received_at,
            "email_thread_messages": str(thread_messages),
            "scope": scope,
            "content_type": content_type,
        }
        # P2 citation: the Outlook deep link to this message. Graph only
        # returns it when the caller asks for webLink, and without it mail
        # captures came out with an empty source_url — a citation that was
        # available and thrown away.
        if web_link:
            metadata["message_url"] = web_link

        try:
            async with httpx.AsyncClient(timeout=_ingest_timeout()) as client:
                resp = await client.post(
                    f"{_api_url()}/api/v1/ingest",
                    headers=api_headers(),
                    json={
                        "content": text,
                        "source": "email",
                        "tenant_id": _tenant(),
                        "metadata": metadata,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return [data.get("id", "")] if data.get("should_save") else []
        except Exception as e:
            logger.error("Ingest API call failed (%s): %s",
                         type(e).__name__, e or "<no message>")
            raise

    async def _resolve_wing(self, sender: str, recipients: list | None = None) -> str:
        """Map an email to a palace wing via the participants' departments.

        Palace model: wing = team/department. Resolution order:
        1. sender's department (Graph, User.Read.All)
        2. first recipient with a department (a conversation with a
           dept-less sender still belongs to the recipients' wing)
        3. fallback "email" wing

        Lookups are cached per address; Graph failures degrade gracefully.
        """
        if not self.graph:
            return "email"

        for addr in [sender] + list(recipients or []):
            if not addr:
                continue
            if addr in self._wing_cache:
                # Only a real wing satisfies the lookup; a cached "email"
                # (unknown user) must not short-circuit the recipient
                # fallback. (Fixed 2026-08-07: caching Admin->email hid
                # Adele's Retail wing from the same lookup.)
                if self._wing_cache[addr] != "email":
                    return self._wing_cache[addr]
                continue
            wing = await self._lookup_department(addr)
            self._wing_cache[addr] = wing
            if wing != "email":
                return wing
        return "email"

    async def _lookup_department(self, address: str) -> str:
        """Resolve one address to its department via Graph (cached)."""
        try:
            data = await self.graph._request(
                "GET",
                f"/users/{address}",
                params={"$select": "department,userPrincipalName"},
            )
            dept = (data.get("department") or "").strip()
            return dept if dept else "email"
        except Exception as e:
            logger.warning("Wing lookup failed for %s: %s", address, e)
            return "email"

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
