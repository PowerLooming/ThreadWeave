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
import os
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
        adapter: object | None = None,
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
        self._adapter = adapter  # BotFrameworkAdapter/CloudAdapter for proactive DMs
        self._pending: dict[str, tuple[DetectionResult, str]] = {}  # (result, original_text)
        self.stats = {"detected": 0, "saved": 0, "ignored": 0, "prompted": 0}
        self._notify_task: asyncio.Task | None = None
        self._notify_interval = float(
            os.environ.get("THREADWEAVE_NOTIFY_INTERVAL", "60")
        )
        self._notify_enabled = os.environ.get(
            "THREADWEAVE_NOTIFY_ENABLED", "1"
        ) not in ("0", "false", "False")
        self._notify_max_attempts = int(
            os.environ.get("THREADWEAVE_NOTIFY_MAX_ATTEMPTS", "5")
        )
        self._notify_failures: dict[str, int] = {}
        self._notify_email_enabled = os.environ.get(
            "THREADWEAVE_NOTIFY_EMAIL", "1"
        ) not in ("0", "false", "False")
        self._notify_sender = os.environ.get("THREADWEAVE_NOTIFY_SENDER", "")
        self._graph_client = None  # lazy, for activity-feed delivery
        self._bot_id = os.environ.get("MICROSOFT_APP_ID", "")
        self.rsc_status: dict[str, dict] = {}  # team id -> consent probe result

    # ---- Capture notification poller ("camera sign" DMs) ----

    def start_notification_poller(self) -> None:
        """Start the background task that DMs authors when their content
        is captured by the daemons."""
        if not self._notify_enabled:
            logger.info("Capture notifications disabled "
                        "(THREADWEAVE_NOTIFY_ENABLED=0)")
            return
        self._notify_task = asyncio.create_task(self._notification_loop())

    async def stop_notification_poller(self) -> None:
        if self._notify_task:
            self._notify_task.cancel()
            try:
                await self._notify_task
            except asyncio.CancelledError:
                pass

    async def _notification_loop(self) -> None:
        while True:
            try:
                await self._deliver_pending_notifications()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Notification poll failed: %s", exc)
            await asyncio.sleep(self._notify_interval)

    async def _deliver_pending_notifications(self) -> None:
        """Fetch undelivered notifications and deliver to their authors.

        Delivery chain per notification:
        1. Personal DM via the stored 1:1 conversation ref (only for
           authors who talked to the bot, and only when the ref really
           is a personal conversation — channel refs must never receive
           capture notices).
        2. Activity-feed notification via Graph (TeamsActivity.Send)
           for everyone else — this is the camera sign for authors
           captured passively (teams-watch, email, SharePoint) who
           never interacted with the bot.
        3. Email fallback via Graph sendMail (Mail.Send) for tenants
           that refuse TeamsActivity.Send, or when the activity
           notification fails (THREADWEAVE_NOTIFY_SENDER required).
        4. After THREADWEAVE_NOTIFY_MAX_ATTEMPTS failures the
           notification is marked skipped (stops retrying, counted in
           stats, not reported as delivered).
        """
        from threadweave.connectors.teams.conversations import (
            get_conversation_store,
        )

        data = await self._api_get("/api/v1/notifications/pending")
        if not data:
            return
        store = get_conversation_store()
        for notif in data.get("notifications", []):
            try:
                await self._deliver_one(notif, store)
            except Exception as exc:
                logger.warning("Notify delivery failed for %s: %s",
                               notif.get("id"), exc)

    async def _deliver_one(self, notif: dict, store) -> None:
        """Deliver one notification (DM or activity feed) and ack it."""
        nid = notif.get("id", "")
        author = notif.get("author_id", "")
        ref = store.get(author)
        if ref and not self._ref_is_personal(ref):
            ref = None  # channel/group refs are not private delivery targets
        if not ref and "@" in author:
            aad_id = await self._resolve_aad_id(author)
            if aad_id:
                ref = store.get(aad_id)
                if ref and not self._ref_is_personal(ref):
                    ref = None

        if ref:
            sent = await self._send_capture_notification(notif, ref)
        else:
            # Passive author: activity-feed notification via Graph,
            # email fallback when that is unavailable or fails.
            target = author
            if "@" in target:
                target = await self._resolve_aad_id(author)
            sent = await self._send_activity_notification(notif, target)
            if not sent:
                email = await self._resolve_author_email(author)
                sent = await self._send_email_notification(notif, email)

        if sent:
            await self._api_post(
                f"/api/v1/notifications/{nid}/delivered", {}
            )
            self.stats["notified"] = self.stats.get("notified", 0) + 1
            self._notify_failures.pop(nid, None)
            return

        attempts = self._notify_failures.get(nid, 0) + 1
        self._notify_failures[nid] = attempts
        if attempts >= self._notify_max_attempts:
            await self._api_post(
                f"/api/v1/notifications/{nid}/delivered?status=skipped", {}
            )
            self.stats["notify_skipped"] = (
                self.stats.get("notify_skipped", 0) + 1
            )
            logger.warning(
                "Notification %s undeliverable after %d attempts — marked "
                "skipped (author %s)", nid, attempts, author,
            )

    @staticmethod
    def _ref_is_personal(ref: dict) -> bool:
        """Only 1:1 conversations may receive capture DMs.

        A ref captured from a channel/group-chat message points at that
        conversation; continue_conversation on it would post the
        capture notice into the channel where everyone sees it. Treat
        anything not explicitly personal as non-DMable (missing type is
        allowed for legacy stores, which predate group refs).
        """
        return ref.get("conversation_type", "") in ("", "personal")

    async def _send_activity_notification(
        self, notif: dict, aad_id: str
    ) -> bool:
        """Activity-feed notification via Graph (TeamsActivity.Send).

        The camera sign for passively captured authors who never talked
        to the bot. Requires the AZURE_* app registration to hold
        TeamsActivity.Send (application). Returns False on failure.
        """
        if not aad_id:
            logger.warning(
                "Notification %s: no AAD identity for %r — cannot notify",
                notif.get("id"), notif.get("author_id"),
            )
            return False
        # Sender identity matters: Graph only allows custom text
        # notifications from the app the recipient has installed, which
        # is the Teams app backed by the bot's own registration
        # (MICROSOFT_APP_ID). Calling as the Graph daemon app gets
        # 403 "not authorized to generate custom text notifications"
        # (live 2026-08-17). Use the bot identity when available.
        bot_app_id = os.environ.get("MICROSOFT_APP_ID", "")
        bot_secret = os.environ.get("MICROSOFT_APP_PASSWORD", "")
        graph = None
        if bot_app_id and bot_secret:
            try:
                from threadweave.connectors.sharepoint.watcher import (
                    GraphClient,
                )

                graph = GraphClient(
                    client_id=bot_app_id, client_secret=bot_secret
                )
            except Exception as exc:
                logger.warning(
                    "Bot-identity Graph client unavailable (%s); "
                    "falling back to daemon identity", exc,
                )
        if graph is None:
            graph = self._get_graph_client()
        if graph is None:
            return False
        title = notif.get("title") or "content"
        # Graph requires a Teams deep link (teams.microsoft.com/l/...)
        # when the topic source is text. Teams captures carry the real
        # message link; other sources get a valid generic deep link.
        web_url = notif.get("message_url") or (
            "https://teams.microsoft.com/l/chat/0/0"
        )
        payload = {
            "topic": {
                "source": "text",
                "value": "ThreadWeave capture",
                "webUrl": web_url,
            },
            "activityType": "systemDefault",
            "previewText": {
                "content": (
                    f"ThreadWeave captured your Teams message "
                    f"\"{title}\" (wing: {notif.get('wing', '')}, "
                    f"room: {notif.get('room', '')}). "
                    f"Message the ThreadWeave bot: 'delete {title}' removes "
                    f"it, 'opt out' stops future captures."
                )
            },
            "templateParameters": [
                {"name": "title", "value": "Captured to the palace"},
            ],
        }
        try:
            await graph._request(
                "POST",
                f"/users/{aad_id}/teamwork/sendActivityNotification",
                json_body=payload,
            )
            logger.info(
                "Activity notification sent to %s (entry %s)",
                aad_id, notif.get("entry_id"),
            )
            return True
        except Exception as exc:
            status = ""
            try:
                status = f" HTTP {exc.response.status_code}"
            except Exception:
                pass
            logger.warning(
                "Activity notification failed for %s%s: %s",
                notif.get("id"), status, exc,
            )
            if "403" in status:
                logger.warning(
                    "Hint: grant TeamsActivity.Send (application) on the "
                    "AZURE_CLIENT_ID app registration and re-consent."
                )
            return False

    async def _resolve_author_email(self, author_id: str) -> str:
        """Best-effort email address for an author id (email or AAD id)."""
        if "@" in (author_id or ""):
            return author_id
        graph = self._get_graph_client()
        if graph is None:
            return ""
        try:
            data = await graph._request(
                "GET", f"/users/{author_id}",
                params={"$select": "mail,userPrincipalName"},
            )
            return data.get("mail") or data.get("userPrincipalName") or ""
        except Exception as exc:
            logger.warning(
                "Email resolution failed for %s: %s", author_id, exc
            )
            return ""

    async def _send_email_notification(
        self, notif: dict, author_email: str
    ) -> bool:
        """Email fallback for the camera sign via Graph sendMail.

        Used when the activity-feed path is unavailable (tenant refuses
        TeamsActivity.Send) or failed. Requires Mail.Send (application)
        on the AZURE_* app registration and THREADWEAVE_NOTIFY_SENDER
        set to a mailbox the app may send from. Returns False on
        failure or when email delivery is disabled/unconfigured.
        """
        if not self._notify_email_enabled:
            return False
        if not author_email or "@" not in author_email:
            return False
        sender = self._notify_sender or os.environ.get(
            "THREADWEAVE_EMAIL_MAILBOX", ""
        )
        if not sender:
            logger.warning(
                "THREADWEAVE_NOTIFY_SENDER not set — email fallback "
                "disabled (notification %s)", notif.get("id"),
            )
            return False
        graph = self._get_graph_client()
        if graph is None:
            return False

        # Recipient guard: only send when the address belongs to a
        # tenant user. Sending to anything else produces NDRs into the
        # sender mailbox and burns the tenant's external-recipient
        # quota (live incident 2026-08-16 with fake test authors).
        try:
            from urllib.parse import quote

            lookup = await graph._request(
                "GET",
                f"/users?$filter=mail eq '{quote(author_email)}'&$select=id",
            )
            if not lookup.get("value"):
                logger.info(
                    "Email notification skipped for %s: '%s' is not a "
                    "tenant user",
                    notif.get("id"), author_email,
                )
                return False
        except Exception as exc:
            logger.warning(
                "Tenant lookup for %s failed: %s", author_email, exc
            )
            return False

        from html import escape

        title = escape(notif.get("title") or "content")
        source = escape(notif.get("source") or "content")
        wing = escape(notif.get("wing") or "")
        room = escape(notif.get("room") or "")
        body_html = (
            f"<p>ThreadWeave captured your Teams message "
            f"&quot;{title}&quot; to the palace "
            f"(wing: {wing}, room: {room}).</p>"
            f"<p>This is an automated capture notice. In Teams, message "
            f"the ThreadWeave bot: <b>delete {title}</b> removes the "
            f"entry, <b>opt out</b> stops future captures.</p>"
        )
        payload = {
            "message": {
                "subject": f"ThreadWeave captured your Teams message",
                "body": {"contentType": "html", "content": body_html},
                "toRecipients": [
                    {"emailAddress": {"address": author_email}}
                ],
            }
        }
        try:
            await graph._request(
                "POST", f"/users/{sender}/sendMail", json_body=payload
            )
            self.stats["notify_email"] = self.stats.get("notify_email", 0) + 1
            logger.info(
                "Email capture notification sent to %s (entry %s)",
                author_email, notif.get("entry_id"),
            )
            return True
        except Exception as exc:
            status = ""
            try:
                status = f" HTTP {exc.response.status_code}"
            except Exception:
                pass
            logger.warning(
                "Email capture notification failed for %s%s: %s",
                notif.get("id"), status, exc,
            )
            if "403" in status:
                logger.warning(
                    "Hint: grant Mail.Send (application) on the "
                    "AZURE_CLIENT_ID app registration and re-consent."
                )
            return False

    # ---- RSC consent probe (no silent @mention-only mode) ----

    def _remember_team(self, activity) -> None:
        """Record the activity's team id and probe consent if it is new."""
        cd = getattr(activity, "channel_data", None) or {}
        if not isinstance(cd, dict):
            return
        team = cd.get("team") or {}
        if not isinstance(team, dict):
            team = {}
        # Channel-scoped installs put the CHANNEL id (19:...@thread.tacv2)
        # in team.id; the real team GUID is aadGroupId. Graph's
        # /teams/{id}/permissionGrants wants the GUID.
        team_id = (team.get("aadGroupId") or team.get("id") or "").strip()
        if not team_id:
            return
        from threadweave.connectors.teams import rsc

        if not rsc.TEAM_GUID_RE.match(team_id):
            # No aadGroupId and team.id is a channel id: nothing
            # verifiable to record. Skip instead of polluting the seen
            # store with an id the probe can never check.
            logger.debug(
                "Skipping non-GUID team id %s (no aadGroupId in activity)",
                team_id[:30],
            )
            return
        from threadweave.connectors.teams.rsc import TeamSeenStore

        if TeamSeenStore().add(team_id):
            logger.info("New team observed: %s — probing RSC consent", team_id)
            try:
                asyncio.create_task(self._probe_new_team(team_id))
            except Exception as exc:
                logger.warning("RSC probe scheduling failed: %s", exc)

    async def check_rsc_consent(self) -> None:
        """Probe RSC consent for every known team; warn when missing.

        Runs at startup (adapter hook). Without consent the bot only
        receives @mentions, so capture degrades silently; this makes it
        loud. Results land in self.rsc_status and the /health endpoint.
        """
        if not self._bot_id:
            logger.warning(
                "RSC consent check skipped: MICROSOFT_APP_ID missing"
            )
            return
        graph = self._get_graph_client()
        if graph is None:
            logger.warning(
                "RSC consent check skipped: AZURE_* credentials missing"
            )
            return
        from threadweave.connectors.teams.rsc import (
            TeamSeenStore, check_team_consent,
        )

        teams = TeamSeenStore().all()
        if not teams:
            logger.info(
                "RSC consent check skipped: no teams observed yet "
                "(add the bot to a team and restart, or wait for the "
                "first activity)"
            )
            return
        for team_id in teams:
            try:
                result = await check_team_consent(
                    graph, team_id, self._bot_id
                )
            except Exception as exc:
                result = {
                    "team_id": team_id, "status": "error",
                    "permissions": [],
                    "detail": f"consent probe failed: {exc}",
                }
            self.rsc_status[team_id] = result
            self._log_consent_result(result)

    async def _probe_new_team(self, team_id: str) -> None:
        """Fire-and-forget consent probe for a newly observed team."""
        try:
            if not self._bot_id:
                return
            graph = self._get_graph_client()
            if graph is None:
                return
            from threadweave.connectors.teams.rsc import check_team_consent

            result = await check_team_consent(graph, team_id, self._bot_id)
            self.rsc_status[team_id] = result
            self._log_consent_result(result)
        except Exception as exc:
            logger.warning("RSC probe for team %s failed: %s", team_id, exc)

    @staticmethod
    def _log_consent_result(result: dict) -> None:
        team_id = result.get("team_id", "?")
        status = result.get("status")
        if status == "granted":
            logger.info(
                "RSC consent verified for team %s: %s",
                team_id, ", ".join(result.get("permissions", [])) or "grant",
            )
        elif status == "missing":
            logger.warning(
                "RSC consent MISSING for team %s — the bot only receives "
                "@mentions there. %s",
                team_id, result.get("detail", ""),
            )
        else:
            logger.warning(
                "RSC consent check for team %s failed: %s",
                team_id, result.get("detail", ""),
            )

    def _get_graph_client(self):
        """Lazily build the app-only Graph client for activity delivery."""
        if self._graph_client is None:
            tenant = os.environ.get("AZURE_TENANT_ID", "")
            client_id = os.environ.get("AZURE_CLIENT_ID", "")
            secret = os.environ.get("AZURE_CLIENT_SECRET", "")
            if not (tenant and client_id and secret):
                logger.warning(
                    "AZURE_* credentials missing — activity-feed "
                    "notifications disabled"
                )
                return None
            from threadweave.connectors.sharepoint.watcher import GraphClient

            self._graph_client = GraphClient(tenant, client_id, secret)
        return self._graph_client

    async def _resolve_aad_id(self, email: str) -> str:
        """Resolve an email to an AAD object id via Graph (GraphReader
        app credentials from the environment). Returns '' on failure."""
        if not email or "@" not in email:
            return ""
        try:
            from msal import ConfidentialClientApplication

            tenant = os.environ.get("AZURE_TENANT_ID", "")
            client_id = os.environ.get("AZURE_CLIENT_ID", "")
            secret = os.environ.get("AZURE_CLIENT_SECRET", "")
            if not (tenant and client_id and secret):
                return ""
            app = ConfidentialClientApplication(
                client_id=client_id,
                client_credential=secret,
                authority=f"https://login.microsoftonline.com/{tenant}",
            )
            result = app.acquire_token_for_client(
                scopes=["https://graph.microsoft.com/.default"]
            )
            if "access_token" not in result:
                return ""
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/users/{email}",
                    headers={"Authorization": f"Bearer {result['access_token']}"},
                )
                if resp.status_code == 200:
                    return str(resp.json().get("id", ""))
        except Exception as exc:
            logger.warning("AAD id resolution failed for %s: %s", email, exc)
        return ""

    async def _send_capture_notification(
        self, notif: dict, ref: dict
    ) -> bool:
        """Send one proactive DM to the content author via the adapter.

        Uses the stored conversation reference (captured when the
        author last talked to the bot) with continue_conversation.
        """
        if self._adapter is None:
            logger.warning("No adapter configured — cannot send proactive DM")
            return False
        try:
            from botbuilder.schema import (
                ChannelAccount, ConversationAccount, ConversationReference,
            )

            reference = ConversationReference(
                activity_id=ref.get("activity_id", ""),
                user=ChannelAccount(
                    id=ref.get("user_id", ""),
                    aad_object_id=ref.get("user_aad_id", ""),
                ),
                bot=ChannelAccount(
                    id=ref.get("bot_id", "") or self._bot_id,
                    name=ref.get("bot_name", ""),
                ),
                conversation=ConversationAccount(
                    id=ref.get("conversation_id", ""),
                    conversation_type=ref.get("conversation_type", ""),
                ),
                channel_id=ref.get("channel_id", "msteams"),
                service_url=ref.get("service_url", ""),
            )

            async def send_capture(turn_context) -> None:
                await turn_context.send_activity(
                    f"**Captured to the palace.** Your Teams message "
                    f"\"{notif.get('title', '')}\" was added "
                    f"(wing: {notif.get('wing', '')}, "
                    f"room: {notif.get('room', '')}). "
                    f"Reply **delete {notif.get('title', '')}** to remove it, "
                    f"or **opt out** to stop future captures."
                )

            bot_app_id = os.environ.get("MICROSOFT_APP_ID", "")
            if hasattr(self._adapter, "continue_conversation"):
                await self._adapter.continue_conversation(
                    reference, send_capture, bot_app_id=bot_app_id
                )
            else:
                await self._adapter.continue_conversation(
                    reference, send_capture
                )
            logger.info("Capture notification DM sent to %s (entry %s)",
                        notif.get("author_id"), notif.get("entry_id"))
            return True
        except Exception as exc:
            logger.warning("Proactive DM failed: %s", exc)
            return False

    # ---- Lifecycle ----

    async def on_members_added_activity(
        self, members_added: list[ChannelAccount], turn_context: TurnContext
    ):
        """Send welcome message when bot is added to a team."""
        self._remember_team(turn_context.activity)
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

        # Track the team for the RSC consent probe.
        self._remember_team(activity)

        # Remember the conversation so we can DM this person later
        # ("camera sign" capture notifications).
        try:
            from threadweave.connectors.teams.conversations import (
                get_conversation_store,
            )

            fp = activity.from_property
            person = getattr(fp, "aad_object_id", "") or getattr(fp, "id", "")
            if person and activity.conversation and activity.service_url:
                get_conversation_store().remember(
                    person_id=str(person),
                    conversation_id=activity.conversation.id,
                    service_url=activity.service_url,
                    channel_id=activity.channel_id or "msteams",
                    name=getattr(fp, "name", "") or "",
                    activity=activity,
                )
        except Exception as exc:
            logger.warning("Conversation capture failed: %s", exc)

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

        # Privacy commands — the "camera sign" layer. Work when mentioned
        # OR in a 1:1 DM with the bot.
        handled = await self._handle_privacy_command(turn_context, activity, text)
        if handled:
            return

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

    # ---- Privacy commands ("camera sign" layer) ----

    async def _handle_privacy_command(
        self, turn_context: TurnContext, activity: Activity, text: str
    ) -> bool:
        """Handle opt-out/opt-in/delete/status commands. Returns True if handled.

        Commands (work via @mention or in a 1:1 DM):
          opt out              — stop harvesting my content
          opt in               — resume harvesting
          delete <topic>       — delete my entries matching <topic>
          status               — show whether I'm opted out + entry count
        """
        lower = text.lower()
        person = self._person_identity(activity)
        if not person:
            return False

        if "opt out" in lower:
            await self._api_post(
                "/api/v1/optout/out", {"person": person}
            )
            await turn_context.send_activity(
                "You're now opted out. I won't save knowledge from your "
                "messages, emails, or documents anymore. Say 'opt in' "
                "anytime to resume. (Existing entries stay until you "
                "delete them.)"
            )
            return True

        if "opt in" in lower:
            await self._api_post(
                "/api/v1/optout/in", {"person": person}
            )
            await turn_context.send_activity(
                "Welcome back — you're opted in again. I'll capture "
                "knowledge from your content as before."
            )
            return True

        if lower.startswith("delete") or "delete " in lower:
            topic = self._strip_trigger_phrases(text)
            topic = topic.replace("delete", "", 1).strip() if topic else ""
            if not topic:
                await turn_context.send_activity(
                    "What should I delete? Try 'delete <topic>', e.g. "
                    "'delete Azure Functions'."
                )
                return True
            await self._handle_delete_command(turn_context, person, topic)
            return True

        if lower.strip() in ("status", "status?"):
            state = await self._api_get("/api/v1/optout")
            opted = state.get("opted_out", []) if state else []
            status = "opted OUT" if person.lower() in opted else "opted in"
            await turn_context.send_activity(
                f"Privacy status: {status}. "
                "Commands: 'opt out', 'opt in', 'delete <topic>', 'status'."
            )
            return True

        return False

    async def _handle_delete_command(
        self, turn_context: TurnContext, person: str, topic: str
    ) -> None:
        """Delete the requester's entries matching a topic."""
        results = await self._api_search(topic)
        mine = [
            r for r in results
            if (r.get("author_id") or "").lower() == person.lower()
        ]
        if not mine:
            await turn_context.send_activity(
                f"No entries by you matched '{topic}'."
            )
            return

        deleted = 0
        for r in mine:
            ok = await self._api_delete(r["id"], person=person)
            if ok:
                deleted += 1
        await turn_context.send_activity(
            f"Deleted {deleted} of {len(mine)} matching entries. "
            "Deletions are permanent and audited."
        )

    @staticmethod
    def _person_identity(activity: Activity) -> str:
        """Best-effort person identity from a Teams activity."""
        fp = activity.from_property
        if not fp:
            return ""
        # AAD object id is the most stable identity the API can match
        # against author_id claims.
        ident = getattr(fp, "aad_object_id", "") or getattr(fp, "id", "")
        return str(ident).strip()

    # ---- API helpers ----

    async def _api_post(self, path: str, body: dict) -> dict | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.api_base_url}{path}", json=body
                )
                if resp.status_code < 300:
                    return resp.json()
                logger.warning("POST %s -> %d", path, resp.status_code)
        except Exception as e:
            logger.warning("POST %s failed: %s", path, e)
        return None

    async def _api_get(self, path: str) -> dict | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.api_base_url}{path}")
                if resp.status_code < 300:
                    return resp.json()
        except Exception as e:
            logger.warning("GET %s failed: %s", path, e)
        return None

    async def _api_search(self, query: str) -> list[dict]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.api_base_url}/api/v1/search",
                    json={"query": query, "limit": 20},
                )
                if resp.status_code < 300:
                    return resp.json().get("results", [])
        except Exception as e:
            logger.warning("search failed: %s", e)
        return []

    async def _api_delete(self, entry_id: str, person: str) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(
                    f"{self.api_base_url}/api/v1/entries/{entry_id}",
                    params={"person_id": person, "role": "readwrite"},
                )
                return resp.status_code == 204
        except Exception as e:
            logger.warning("delete %s failed: %s", entry_id, e)
        return False

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
        ctx = self._conversation_context(activity)
        self._pending[entry_id] = (result, text, ctx)

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

        ctx = self._conversation_context(activity)
        self._pending[entry_id] = (result, text, ctx)
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

        detection, original_text, ctx = stored

        try:
            saved = await self._save_to_api(
                content=original_text,
                content_type=detection.content_type.value,
                scope=detection.suggested_scope,
                title=detection.suggested_title,
                confidence=detection.confidence,
                wing=ctx.get("wing", "general"),
                room=ctx.get("room", ""),
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

        detection, _, _ = stored
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
        wing: str = "general",
        room: str = "",
    ) -> dict:
        """Save knowledge via central ingestion pipeline POST /api/v1/ingest."""
        import httpx

        if not room:
            room = content_type

        payload = {
            "content": content,
            "source": "teams",
            "tenant_id": "default",
            "metadata": {
                "wing": wing,
                "room": room,
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
                        "wing": wing,
                        "room": room,
                        "scope": scope,
                        "source_type": "teams",
                        "title": title,
                        "tenant_id": "default",
                    },
                )
                resp.raise_for_status()
                return resp.json()

        return result

    # ---- Conversation Context ----

    def _conversation_context(self, activity: Activity) -> dict:
        """Derive ThreadWeave wing/room from the Teams conversation.

        Channel message  → wing = team name, room = channel name
        Group chat       → wing = "general", room = "group-chat"
        DM               → wing = "general", room = "dm"

        Falls back to the default wing "general" when no team context
        exists (DMs, group chats) — the palace model expects wing=team,
        and untagged captures land in the general wing.
        """
        cd = getattr(activity, "channel_data", None) or {}
        if not isinstance(cd, dict):
            cd = {}

        chat_type = cd.get("chatType", "")

        if chat_type == "channel":
            team = cd.get("team", {}) or {}
            channel = cd.get("channel", {}) or {}
            wing = (team.get("name") or "general").strip() or "general"
            room = (channel.get("name") or "").strip() or "general"
            return {"wing": wing, "room": room}

        if chat_type == "groupChat":
            return {"wing": "general", "room": "group-chat"}

        return {"wing": "general", "room": "dm"}

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