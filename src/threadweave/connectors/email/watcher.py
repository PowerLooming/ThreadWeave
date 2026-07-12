# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Email Watcher — monitors Exchange Online mailboxes via Microsoft Graph API.

Handles:
    - Mailbox monitoring via Graph API subscriptions and delta queries
    - Shared mailbox support (knowledge@firma.no pattern)
    - Inbound email filtering (only process unread, from known senders, etc.)
    - Thread tracking (conversationId grouping)

Usage:
    watcher = MailWatcher(tenant_id="...", client_id="...", client_secret="...")
    emails = await watcher.fetch_unread(mailbox="knowledge@firma.no")

The CC-me pattern: Users CC knowledge@firma.no on important threads.
ThreadWeave monitors that inbox, extracts the thread, and runs detection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from threadweave.connectors.sharepoint.watcher import (
    GraphClient,
    Subscription,
    GRAPH_API_BASE,
)

logger = logging.getLogger(__name__)

# Maximum emails to process per batch
MAX_EMAILS_PER_BATCH = 50

# Only process emails newer than this (days)
MAX_EMAIL_AGE_DAYS = 30


# ---- Data Classes ----

@dataclass
class EmailMessage:
    """A lightweight email representation from Graph API."""
    message_id: str
    conversation_id: str
    subject: str
    sender_name: str
    sender_email: str
    recipients: list[str] = field(default_factory=list)
    cc_recipients: list[str] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""
    has_attachments: bool = False
    attachment_names: list[str] = field(default_factory=list)
    received_at: str = ""
    is_read: bool = False
    importance: str = "normal"
    thread_position: int = 0  # Position in conversation (1 = first)


@dataclass
class EmailThread:
    """A conversation thread — multiple related emails."""
    conversation_id: str
    subject: str
    messages: list[EmailMessage] = field(default_factory=list)
    participant_count: int = 0
    message_count: int = 0
    started_at: str = ""
    last_activity: str = ""


@dataclass
class FetchResult:
    """Result of fetching and processing emails."""
    mailbox: str
    emails_fetched: int
    threads_found: int
    knowledge_extracted: int  # Emails worth saving
    errors: int = 0
    processed_message_ids: list[str] = field(default_factory=list)


# ---- Mail Watcher ----

class MailWatcher:
    """
    Monitors Exchange Online mailboxes for organizational knowledge.

    The primary use case is a shared mailbox (knowledge@firma.no)
    that users CC on important threads. ThreadWeave monitors this,
    extracts the full thread, and runs detection on the content.

    Graph API permissions needed:
        Mail.Read        - Read mailbox contents
        Mail.ReadWrite   - Mark as read after processing (optional)
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        self.graph = GraphClient(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    # ---- Mailbox Operations ----

    async def fetch_unread(
        self,
        mailbox: str,
        folder: str = "inbox",
        max_results: int = MAX_EMAILS_PER_BATCH,
        max_age_days: int = MAX_EMAIL_AGE_DAYS,
    ) -> list[EmailMessage]:
        """
        Fetch unread emails from a mailbox.

        Args:
            mailbox: Email address or user principal name
            folder: Mail folder (inbox, archive, etc.)
            max_results: Maximum emails to return
            max_age_days: Only process emails newer than this
        """
        # Graph API filters
        since = (datetime.now(timezone.utc) - timedelta(days=max_age_days))
        filter_query = (
            f"isRead eq false and receivedDateTime ge {since.isoformat()}"
        )

        path = (
            f"/users/{mailbox}/mailFolders/{folder}/messages"
            f"?$filter={filter_query}"
            f"&$orderby=receivedDateTime desc"
            f"&$top={max_results}"
            f"&$select=id,conversationId,subject,from,toRecipients,"
            f"ccRecipients,body,hasAttachments,receivedDateTime,"
            f"isRead,importance,internetMessageHeaders"
        )

        data = await self.graph._request("GET", path)
        messages = []

        for item in data.get("value", []):
            messages.append(self._parse_message(item))

        logger.info("Fetched %d unread from %s", len(messages), mailbox)
        return messages

    async def fetch_thread(
        self,
        mailbox: str,
        conversation_id: str,
    ) -> EmailThread:
        """
        Fetch all messages in a conversation thread.

        Important for the CC-me pattern: when someone CCs the
        knowledge mailbox on reply #5, we need the full thread
        (messages 1-5) for context.
        """
        path = (
            f"/users/{mailbox}/messages"
            f"?$filter=conversationId eq '{conversation_id}'"
            f"&$orderby=receivedDateTime asc"
            f"&$top=100"
            f"&$select=id,conversationId,subject,from,toRecipients,"
            f"ccRecipients,body,hasAttachments,receivedDateTime,"
            f"isRead,importance"
        )

        data = await self.graph._request("GET", path)
        messages = [self._parse_message(item) for item in data.get("value", [])]

        if not messages:
            return EmailThread(
                conversation_id=conversation_id,
                subject="",
            )

        thread = EmailThread(
            conversation_id=conversation_id,
            subject=messages[0].subject,
            messages=messages,
            message_count=len(messages),
            started_at=messages[0].received_at,
            last_activity=messages[-1].received_at,
        )

        # Count unique participants
        participants = set()
        for msg in messages:
            participants.add(msg.sender_email)
            participants.update(msg.recipients)
            participants.update(msg.cc_recipients)
        thread.participant_count = len(participants)

        return thread

    async def mark_as_read(self, mailbox: str, message_ids: list[str]):
        """Mark processed emails as read."""
        for mid in message_ids:
            try:
                await self.graph._request(
                    "PATCH",
                    f"/users/{mailbox}/messages/{mid}",
                    json_body={"isRead": True},
                )
            except Exception as e:
                logger.warning("Failed to mark %s as read: %s", mid, e)

    # ---- Subscription Management ----

    async def create_mail_subscription(
        self,
        mailbox: str,
        notification_url: str,
        client_state: str = "",
        folder: str = "inbox",
    ) -> Subscription:
        """
        Create a Graph API subscription for new mail in a folder.

        Microsoft sends a POST to notification_url when new mail
        arrives. ThreadWeave then fetches and processes.
        """
        expiration = datetime.now(timezone.utc) + timedelta(days=3)
        resource = f"/users/{mailbox}/mailFolders/{folder}/messages"

        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration.isoformat(),
            "clientState": client_state,
        }

        data = await self.graph._request(
            "POST", "/subscriptions", json_body=body
        )

        sub = Subscription(
            subscription_id=data["id"],
            resource=data["resource"],
            notification_url=data["notificationUrl"],
            expiration=datetime.fromisoformat(
                data["expirationDateTime"].replace("Z", "+00:00")
            ),
            client_state=client_state,
        )

        logger.info("Mail subscription: %s -> %s", sub.subscription_id, mailbox)
        return sub

    # ---- Helpers ----

    def _parse_message(self, item: dict) -> EmailMessage:
        """Parse Graph API message JSON into EmailMessage."""
        sender = item.get("from", {}).get("emailAddress", {})
        to_list = [
            r.get("emailAddress", {}).get("address", "")
            for r in item.get("toRecipients", [])
        ]
        cc_list = [
            r.get("emailAddress", {}).get("address", "")
            for r in item.get("ccRecipients", [])
        ]

        # Extract text body
        body = item.get("body", {})
        body_text = ""
        body_html = ""
        if body.get("contentType") == "text":
            body_text = body.get("content", "")
        else:
            body_html = body.get("content", "")

        # Get attachment info
        has_attachments = item.get("hasAttachments", False)
        attachment_names = []

        return EmailMessage(
            message_id=item.get("id", ""),
            conversation_id=item.get("conversationId", ""),
            subject=item.get("subject", ""),
            sender_name=sender.get("name", ""),
            sender_email=sender.get("address", ""),
            recipients=to_list,
            cc_recipients=cc_list,
            body_text=body_text,
            body_html=body_html,
            has_attachments=has_attachments,
            attachment_names=attachment_names,
            received_at=item.get("receivedDateTime", ""),
            is_read=item.get("isRead", False),
            importance=item.get("importance", "normal"),
        )

    async def fetch_attachments(
        self, mailbox: str, message_id: str
    ) -> list[dict]:
        """Fetch attachment metadata for a message."""
        path = (
            f"/users/{mailbox}/messages/{message_id}/attachments"
        )
        data = await self.graph._request("GET", path)
        return data.get("value", [])

    async def download_attachment(
        self, mailbox: str, message_id: str, attachment_id: str
    ) -> bytes:
        """Download attachment content."""
        # Graph API serves attachment content via $value
        token = await self.graph._get_token()
        url = (
            f"{GRAPH_API_BASE}/users/{mailbox}/messages/"
            f"{message_id}/attachments/{attachment_id}/$value"
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            import httpx
            MSAL_AVAILABLE = True
        except ImportError:
            MSAL_AVAILABLE = False

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content
