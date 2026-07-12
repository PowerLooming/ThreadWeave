# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Gmail Watcher — captures knowledge from Gmail messages.

Polls the Gmail inbox for recent messages, extracts text bodies,
threads replies, and submits promising content to the ThreadWeave
ingestion pipeline.

Uses Gmail API v1: users.messages.list + users.messages.get

Design:
    - Poll-based (Gmail push via Pub/Sub is enterprise-only)
    - Tracks last processed timestamp to avoid re-processing
    - Extracts plain text from HTML emails
    - Reconstructs thread context for replies
    - Only submits content that passes the detection engine
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from typing import Optional

import requests

from threadweave.connectors.gws.auth import GWSAuth

logger = logging.getLogger("threadweave.gws.gmail")


@dataclass
class GmailMessage:
    """A parsed Gmail message ready for knowledge extraction."""
    message_id: str
    thread_id: str
    sender: str
    recipients: list[str]
    subject: str
    body: str           # Plain text body
    snippet: str        # Gmail snippet
    timestamp: str      # ISO 8601
    labels: list[str] = field(default_factory=list)
    is_thread_root: bool = False


class GmailWatcher:
    """Watch a Gmail inbox for knowledge-worthy messages.

    Polls the Gmail API for recent messages, extracts content,
    and submits to the ThreadWeave ingestion pipeline.
    """

    # Max results per API call
    MAX_RESULTS = 50

    def __init__(
        self,
        auth: GWSAuth,
        threadweave_url: str = "http://localhost:8000",
    ):
        self.auth = auth
        self.threadweave_url = threadweave_url.rstrip("/")
        self._last_timestamp: Optional[str] = None  # Track last processed
        self._processed_ids: set[str] = set()       # Dedup across polls

    # ── Fetch ─────────────────────────────────────────────────────

    def fetch_recent(
        self, max_results: int = MAX_RESULTS, query: str = ""
    ) -> list[GmailMessage]:
        """Fetch recent messages from the inbox.

        Args:
            max_results: Maximum number of messages to return.
            query: Gmail search query (e.g. 'newer_than:2d', 'is:unread').

        Returns:
            List of parsed GmailMessage objects.
        """
        service = self.auth.gmail()
        search_query = query or "in:inbox"
        if self._last_timestamp:
            # Gmail search: after:YYYY/MM/DD
            ts = datetime.fromisoformat(self._last_timestamp)
            search_query += f" after:{ts.strftime('%Y/%m/%d')}"

        try:
            results = (
                service.users()
                .messages()
                .list(userId="me", q=search_query, maxResults=max_results)
                .execute()
            )
        except Exception as exc:
            logger.error("Gmail list failed: %s", exc)
            return []

        messages = results.get("messages", [])
        parsed = []

        for msg in messages:
            msg_id = msg["id"]
            if msg_id in self._processed_ids:
                continue

            try:
                full = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="raw")
                    .execute()
                )
            except Exception as exc:
                logger.warning("Failed to fetch message %s: %s", msg_id, exc)
                continue

            parsed_msg = self._parse_message(full)
            if parsed_msg:
                parsed.append(parsed_msg)
                self._processed_ids.add(msg_id)

        if parsed:
            self._last_timestamp = max(
                p.timestamp for p in parsed if p.timestamp
            ) or self._last_timestamp

        return parsed

    def _parse_message(self, raw: dict) -> Optional[GmailMessage]:
        """Parse a raw Gmail message into a GmailMessage."""
        msg_id = raw.get("id", "")
        thread_id = raw.get("threadId", "")

        # Headers — try structured payload first, then MIME from raw
        headers = {}
        for h in raw.get("payload", {}).get("headers", []):
            headers[h.get("name", "").lower()] = h.get("value", "")

        # If no structured headers, parse from raw MIME
        if not headers and raw.get("raw"):
            headers = self._parse_mime_headers(raw["raw"])

        sender = headers.get("from", "unknown")
        subject = headers.get("subject", "(no subject)")
        timestamp = headers.get("date", "")
        # Normalize timestamp to ISO 8601
        if timestamp:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(timestamp)
                timestamp = dt.isoformat()
            except Exception:
                pass

        # Body: prefer raw decoding for fidelity
        body = self._extract_body(raw)

        # Skip empty or noise
        if not body or len(body.strip()) < 20:
            return None

        return GmailMessage(
            message_id=msg_id,
            thread_id=thread_id,
            sender=sender,
            recipients=self._extract_recipients(headers),
            subject=subject,
            body=body,
            snippet=raw.get("snippet", ""),
            timestamp=timestamp,
            labels=raw.get("labelIds", []),
        )

    def _parse_mime_headers(self, raw_base64: str) -> dict:
        """Parse headers from a base64-encoded raw MIME message."""
        try:
            decoded = base64.urlsafe_b64decode(raw_base64)
            msg = BytesParser(policy=policy.default).parsebytes(decoded)
            headers = {}
            for key in ("from", "to", "cc", "subject", "date"):
                val = msg.get(key, "")
                if val:
                    headers[key] = str(val)
            return headers
        except Exception:
            return {}

    def _extract_body(self, raw: dict) -> str:
        """Extract plain text body from a Gmail message.

        Tries raw base64 decoding first, then falls back to
        the structured payload.
        """
        # Try raw content
        raw_bytes = raw.get("raw")
        if raw_bytes:
            try:
                decoded = base64.urlsafe_b64decode(raw_bytes)
                msg = BytesParser(policy=policy.default).parsebytes(decoded)
                return self._get_plain_text(msg)
            except Exception:
                pass

        # Fall back to payload parts
        payload = raw.get("payload", {})
        return self._extract_payload_text(payload)

    def _get_plain_text(self, msg) -> str:
        """Get plain text from an email.message.Message."""
        if msg.is_multipart():
            parts = []
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        pass
            return "\n".join(parts)
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                payload = msg.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                return ""

    def _extract_payload_text(self, payload: dict) -> str:
        """Recursively extract text from Gmail API payload structure."""
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")

        if mime_type == "text/plain" and body_data:
            try:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            except Exception:
                return ""

        # Recurse into parts
        parts = payload.get("parts", [])
        texts = []
        for part in parts:
            text = self._extract_payload_text(part)
            if text:
                texts.append(text)
        return "\n".join(texts)

    def _extract_recipients(self, headers: dict) -> list[str]:
        """Extract recipient emails from headers."""
        recipients = []
        for field in ("to", "cc", "bcc"):
            value = headers.get(field, "")
            if value:
                # Parse "Name <email>" or "email"
                for addr in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", value):
                    if addr not in recipients:
                        recipients.append(addr)
        return recipients

    # ── Submit to ThreadWeave ────────────────────────────────────

    def submit_messages(
        self, messages: list[GmailMessage],
    ) -> dict:
        """Submit parsed messages to the ThreadWeave ingestion pipeline.

        Returns stats: {submitted: int, saved: int, skipped: int}.
        """
        stats = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}

        for msg in messages:
            stats["submitted"] += 1
            try:
                resp = requests.post(
                    f"{self.threadweave_url}/api/v1/ingest",
                    json={
                        "content": (
                            f"Subject: {msg.subject}\n"
                            f"From: {msg.sender}\n\n"
                            f"{msg.body}"
                        ),
                        "source": "gmail",
                        "metadata": {
                            "message_id": msg.message_id,
                            "thread_id": msg.thread_id,
                            "sender": msg.sender,
                            "recipients": msg.recipients,
                            "subject": msg.subject,
                            "timestamp": msg.timestamp,
                            "wing": "gmail",  # Can be overridden by routing rules
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
                logger.warning("Failed to submit message: %s", exc)

        return stats

    def process_inbox(self, query: str = "") -> dict:
        """Fetch recent inbox messages and submit to ThreadWeave.

        One-shot operation: polls inbox, submits all found messages.

        Args:
            query: Optional Gmail search to filter messages.

        Returns:
            Processing stats.
        """
        messages = self.fetch_recent(query=query)
        return self.submit_messages(messages)
