# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Teams Watch Daemon — passive capture of Teams channel messages.

The Teams bot can only see conversations it was invited into
(@mention, DM, or RSC-consented teams/chats). This daemon closes the
gap: with Graph application permissions it delta-polls EVERY channel
in EVERY team, including teams where the bot was never installed.

Privacy contract: message content flows ONE WAY, M365 -> on-prem
ThreadWeave, via authenticated Graph API pulls. No webhook, no tunnel,
no third-party relay — the daemon polls Graph's channel-message delta
endpoint on a schedule, so the only network path is outbound from the
on-prem host. The same pattern as `threadweave email watch` and
`threadweave sharepoint watch`.

Required Graph application permissions (tenant admin consent, once):
    ChannelMessage.Read.All — read all channel messages (all teams)
    Team.ReadBasic.All      — enumerate teams and channels

Delta tokens persist to a state file, so restarts resume where the
last poll stopped instead of re-crawling history. On the FIRST poll a
channel has no token; the default "prime" mode skips existing history
(only messages posted after the poll started are captured) unless
--backfill is given.

Usage:
    python -m threadweave.cli teams watch --interval 300
    python -m threadweave.cli teams watch --backfill --max-messages 500
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

from threadweave.connectors.sharepoint.watcher import GRAPH_API_BASE, GraphClient

logger = logging.getLogger(__name__)


def _is_deltatoken_quirk(response: httpx.Response) -> bool:
    """True for Graph's zero-message delta quirk (400 with
    "Parameter 'DeltaToken' not supported" in the error body)."""
    try:
        body = response.json()
    except Exception:
        return False
    return "DeltaToken" in str(body.get("error", {}).get("message", ""))

DEFAULT_INTERVAL = 300               # seconds between polls
DEFAULT_STATE_FILE = "~/.threadweave/teams_delta.json"
DEFAULT_MAX_MESSAGES = 100           # per-channel cap in backfill mode
MIN_TEXT_LENGTH = 50                 # below this no knowledge signal
MAX_TITLE_LENGTH = 80                # snippet used as entry title


@dataclass
class TeamInfo:
    id: str
    display_name: str


@dataclass
class ChannelInfo:
    team_id: str
    team_name: str
    id: str
    display_name: str


class _TextExtractor(HTMLParser):
    """Minimal HTML -> plain text (Teams message bodies are HTML)."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip tags and decode entities from a Teams HTML message body."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html or "")
        return " ".join("".join(extractor.parts).split())
    except Exception:
        return ""


class TeamsGraphClient(GraphClient):
    """Graph client extended with Teams read methods (app-only auth)."""

    async def list_teams(self) -> list[TeamInfo]:
        data = await self._request("GET", "/teams")
        return [
            TeamInfo(
                id=item["id"],
                display_name=item.get("displayName", item.get("name", "")),
            )
            for item in data.get("value", [])
        ]

    async def list_channels(self, team_id: str) -> list[ChannelInfo]:
        data = await self._request("GET", f"/teams/{team_id}/channels")
        return [
            ChannelInfo(
                team_id=team_id,
                team_name="",
                id=item["id"],
                display_name=item.get("displayName", ""),
            )
            for item in data.get("value", [])
        ]

    async def get_channel_messages_delta(
        self,
        team_id: str,
        channel_id: str,
        delta_url: str = "",
    ) -> tuple[list[dict], str]:
        """Delta-poll one channel's messages.

        Follows @odata.nextLink pages and returns (messages, deltaLink).
        The deltaLink is '' when Graph returned no token for this page
        (rare; treat as a transient state).

        Graph quirk (live-verified 2026-08-16): for a channel with zero
        message history, the first delta page is empty but carries a
        @odata.nextLink with a $skiptoken. Following it fails with
        400 "Parameter 'DeltaToken' not supported". The channel's delta
        completes normally once the channel has at least one message, so
        the quirk is tolerated: we return what we have with no deltaLink
        and let the next poll re-prime.
        """
        url = delta_url or (
            f"{GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages/delta"
        )
        messages: list[dict] = []
        delta_link = ""
        while url:
            try:
                data = await self._request_url("GET", url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400 and _is_deltatoken_quirk(
                    exc.response
                ):
                    logger.info(
                        "delta nextLink unsupported for %s/%s (zero-message "
                        "channel sync not ready); re-priming next cycle",
                        team_id, channel_id,
                    )
                    break
                raise
            messages.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
            if data.get("@odata.deltaLink"):
                delta_link = data["@odata.deltaLink"]
        return messages, delta_link


class TeamsWatchDaemon:
    """Poll every team/channel for new messages and mine knowledge.

    Args:
        graph: TeamsGraphClient (list_teams, list_channels,
            get_channel_messages_delta)
        interval: seconds between polls
        state_file: JSON file persisting per-channel delta tokens
        api_base_url: ThreadWeave API server (ingest endpoint)
        backfill: on a channel's first poll, process existing history
            instead of only messages newer than the poll start
        max_messages: per-channel history cap in backfill mode
            (0 = unlimited)
        team_filter: optional display-name substring; only matching
            teams are watched
        ingest: optional async callable(payload) -> dict replacing the
            HTTP ingest post (tests inject a fake here)
    """

    def __init__(
        self,
        graph,
        interval: int = DEFAULT_INTERVAL,
        state_file: str = DEFAULT_STATE_FILE,
        api_base_url: str = "http://localhost:8000",
        backfill: bool = False,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        team_filter: str = "",
        ingest=None,
    ):
        self.graph = graph
        self.interval = max(10, int(interval))
        self.state_file = os.path.expanduser(state_file)
        self.api_base_url = (api_base_url or "").rstrip("/")
        self.backfill = bool(backfill)
        self.max_messages = max(0, int(max_messages))
        self.team_filter = (team_filter or "").strip().lower()
        self.ingest = ingest  # test injection point
        self._state: dict[str, str] = {}
        self._load_state()
        self.stats = {
            "polls": 0,
            "teams": 0,
            "channels": 0,
            "messages_seen": 0,
            "processed": 0,
            "submitted": 0,
            "skipped": 0,
            "errors": 0,
        }

    # ---- Public API ----

    async def run(self) -> None:
        """Run the polling loop until interrupted."""
        print(
            f"Teams watcher (interval={self.interval}s, "
            f"team_filter={self.team_filter or '*'}, "
            f"backfill={self.backfill}, state={self.state_file})"
        )
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                tick = datetime.now(timezone.utc).isoformat()[:19]
                try:
                    result = await self.run_once()
                    if (
                        result["messages_seen"] > 0
                        or result["errors"] > 0
                    ):
                        print(f"[{tick}] {self._summarize(result)}")
                except Exception as e:
                    self.stats["errors"] += 1
                    logger.error("Poll failed: %s", e)
                    print(f"[{tick}] error: {e}")
                await asyncio.sleep(self.interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nTeams watcher stopped.")
            print(f"Total: {self._summarize(self.stats)}")

    async def run_once(self) -> dict:
        """One poll cycle over all teams/channels. Returns a stats dict."""
        self.stats["polls"] += 1
        result = {
            "teams": 0, "channels": 0, "messages_seen": 0,
            "processed": 0, "submitted": 0, "skipped": 0, "errors": 0,
        }
        poll_start = _zulu_now()

        try:
            teams = await self.graph.list_teams()
        except Exception as e:
            self.stats["errors"] += 1
            result["errors"] += 1
            logger.error("list_teams failed: %s", e)
            return result

        for team in teams:
            if self.team_filter and (
                self.team_filter not in team.display_name.lower()
            ):
                continue
            result["teams"] += 1
            self.stats["teams"] += 1
            try:
                channels = await self.graph.list_channels(team.id)
            except Exception as e:
                result["errors"] += 1
                self.stats["errors"] += 1
                logger.error(
                    "list_channels %s failed: %s", team.display_name, e
                )
                continue
            for channel in channels:
                result["channels"] += 1
                self.stats["channels"] += 1
                channel.team_name = team.display_name
                await self._poll_channel(channel, result, poll_start)

        self._save_state()
        return result

    # ---- Internals ----

    async def _poll_channel(
        self, channel: ChannelInfo, result: dict, poll_start: str
    ) -> None:
        key = f"{channel.team_id}/{channel.id}"
        had_token = bool(self._state.get(key))
        try:
            messages, delta_link = await self.graph.get_channel_messages_delta(
                channel.team_id, channel.id, delta_url=self._state.get(key, "")
            )
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error("delta poll %s failed: %s", key, e)
            return

        # First poll of a channel without --backfill: prime the token
        # only, ignore history (nothing "happened" while we watched).
        prime = (not had_token) and (not self.backfill)

        selected = []
        for msg in messages:
            result["messages_seen"] += 1
            self.stats["messages_seen"] += 1
            if prime and (
                msg.get("createdDateTime", "") < poll_start
            ):
                continue
            selected.append(msg)

        if had_token is False and self.backfill and self.max_messages:
            selected = selected[-self.max_messages:]

        for msg in selected:
            await self._process_message(channel, msg, result)

        if delta_link:
            self._state[key] = delta_link

    async def _process_message(
        self, channel: ChannelInfo, msg: dict, result: dict
    ) -> None:
        """Filter, extract, and submit one channel message."""
        skip = self._skip_reason(msg)
        if skip:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        author = (msg.get("from") or {}).get("user") or {}
        author_id = author.get("id", "") or author.get("email", "")

        text = html_to_text((msg.get("body") or {}).get("content", ""))
        if len(text) < MIN_TEXT_LENGTH:
            result["skipped"] += 1
            self.stats["skipped"] += 1
            return

        title = (msg.get("subject") or "").strip()
        if not title:
            title = " ".join(text.split())[:MAX_TITLE_LENGTH]

        payload = {
            "content": text,
            "source": "teams",
            "tenant_id": "default",
            "metadata": {
                "wing": _sanitize(channel.team_name or channel.team_id),
                "room": _sanitize(channel.display_name or channel.id),
                "title": title,
                "author_id": author_id,
                "source_file": msg.get("id", ""),
                "message_url": msg.get("webUrl", ""),
                "created_at": msg.get("createdDateTime", ""),
            },
        }

        try:
            response = await self._submit(payload)
        except Exception as e:
            result["errors"] += 1
            self.stats["errors"] += 1
            logger.error(
                "ingest for message %s failed: %s", msg.get("id"), e
            )
            return

        result["processed"] += 1
        self.stats["processed"] += 1
        if response.get("should_save"):
            result["submitted"] += 1
            self.stats["submitted"] += 1
            logger.info(
                "Captured Teams message %s (%s/%s) -> %s",
                msg.get("id"), channel.team_name, channel.display_name,
                response.get("id"),
            )

    def _skip_reason(self, msg: dict) -> str:
        """Return a reason to skip this message, or '' to process it."""
        msg_type = msg.get("messageType")
        if msg_type and msg_type != "message":
            return "system"  # system events (members added, etc.)
        sender = msg.get("from") or {}
        if sender.get("application"):
            return "bot"  # bot/app posts (incl. our own bot)
        author = sender.get("user") or {}
        author_id = author.get("id", "") or author.get("email", "")
        if not author_id:
            return "no_author"  # unattributable: capture would be anonymous
        from threadweave.optout import OptOutStore

        optout = OptOutStore()
        if optout.any_opted_out(
            [author_id, author.get("email", ""), author.get("displayName", "")]
        ):
            return "optout"  # camera sign: the author declined
        return ""

    async def _submit(self, payload: dict) -> dict:
        """POST to the central ingestion pipeline (or injected fake)."""
        if self.ingest is not None:
            return await self.ingest(payload)
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.api_base_url}/api/v1/ingest", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    # ---- State persistence ----

    def _load_state(self) -> None:
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._state = {k: v for k, v in data.items() if v}
        except Exception as e:
            logger.warning(
                "Failed to load delta state %s: %s", self.state_file, e
            )

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2)
        except Exception as e:
            logger.warning(
                "Failed to save delta state %s: %s", self.state_file, e
            )

    @staticmethod
    def _summarize(stats: dict) -> str:
        return (
            f"teams={stats.get('teams', 0)} "
            f"channels={stats.get('channels', 0)} "
            f"messages={stats.get('messages_seen', 0)} "
            f"processed={stats.get('processed', 0)} "
            f"submitted={stats.get('submitted', 0)} "
            f"skipped={stats.get('skipped', 0)} "
            f"errors={stats.get('errors', 0)}"
        )


def _sanitize(name: str) -> str:
    """Convert a team/channel display name to a palace wing/room name."""
    return (name or "").lower().replace(" ", "_").replace("-", "_")[:64]


def _zulu_now() -> str:
    """Current UTC time as a Z-suffixed ISO string (Graph's format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
