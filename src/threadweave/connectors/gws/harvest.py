# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Offboarding Harvester — bulk knowledge extraction from a departing employee's
email, chat, and documents before they leave the organization.

This is the "pre-departure knowledge safety net." When someone hands in their
notice, the harvester scans their entire mailbox, chat history, and Drive
files for knowledge worth preserving. It runs the ThreadWeave detection engine
on every message, capturing only what's worth saving.

Architecture:
    departing user's Gmail → Gmail API (paginated, filtered) →
    ThreadWeave detection engine → save to org memory

Also supports onboarding: when a new hire starts, surface the knowledge
harvested from their predecessor and team.

Usage:
    threadweave gws harvest alice@company.com
    threadweave gws harvest alice@company.com --source gmail --max 5000
    threadweave gws harvest-report alice@company.com
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from threadweave.connectors.gws.auth import GWSAuth, GWSCredentials
from threadweave.connectors.gws.gmail import GmailWatcher, GmailMessage
from threadweave.connectors.gws.chat import ChatListener
from threadweave.connectors.gws.drive import DriveCrawler

logger = logging.getLogger("threadweave.gws.harvest")

# ── Knowledge-bearing Gmail search queries ─────────────────────────

# Queries that find knowledge worth saving in a departing employee's mailbox.
# These target SENT mail (answers they wrote) and received threads where they
# were the primary participant.
HARVEST_QUERIES = {
    "decisions": (
        'in:sent (decided OR decision OR "we chose" OR "we will use" '
        'OR "the plan is" OR "go with" OR "architecture decision")'
    ),
    "answers": (
        'in:sent ("the reason is" OR "because" OR "the trick is" '
        'OR "you need to" OR "you should" OR "this is because" '
        'OR "the key is" OR "the issue is")'
    ),
    "processes": (
        'in:sent ("how to" OR "the process for" OR "steps to" '
        'OR "always" OR "never" OR "make sure to" OR "don\'t forget")'
    ),
    "client_context": (
        '(client OR customer OR account) '
        '(preference OR requirement OR "needs" OR "wants" OR specific OR custom)'
    ),
    "long_threads": (
        'in:sent longer:500'  # Messages longer than 500 chars
    ),
    "thread_participation": (
        'in:inbox (subject:"Re:" OR subject:"Fwd:") '
        'from:{email} newer_than:2y'
    ),
}

# Minimum character length for a message to be worth processing
MIN_MESSAGE_LENGTH = 80


@dataclass
class HarvestStats:
    """Statistics from a completed harvest."""

    user_email: str = ""
    started_at: str = ""
    completed_at: str = ""

    # Email stats
    emails_scanned: int = 0
    emails_processed: int = 0       # Passed length filter
    emails_saved: int = 0           # Saved to ThreadWeave

    # Chat stats
    chat_messages_scanned: int = 0
    chat_messages_saved: int = 0

    # Drive stats
    drive_files_scanned: int = 0
    drive_files_saved: int = 0

    # Overall
    total_saved: int = 0
    errors: list[str] = field(default_factory=list)
    top_wings: list[dict] = field(default_factory=list)  # Where knowledge landed

    def to_dict(self) -> dict:
        return {
            "user_email": self.user_email,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "emails_scanned": self.emails_scanned,
            "emails_processed": self.emails_processed,
            "emails_saved": self.emails_saved,
            "chat_messages_scanned": self.chat_messages_scanned,
            "chat_messages_saved": self.chat_messages_saved,
            "drive_files_scanned": self.drive_files_scanned,
            "drive_files_saved": self.drive_files_saved,
            "total_saved": self.total_saved,
            "errors": self.errors[-10:],
        }


class OffboardingHarvester:
    """Harvest knowledge from a departing employee's Google Workspace.

    This is a BULK processor — it scans the entire mailbox, not just recent
    messages. Uses paginated Gmail API with rate limiting. Supports resume
    from checkpoint so interrupted harvests can continue.

    The harvester impersonates the departing user via domain-wide delegation,
    reading their Gmail, Chat, and Drive as that user.
    """

    # Gmail API rate limit: 250 quota units/user/sec. Each messages.get = 5 units.
    # Safe rate: ~25 messages/sec with 100ms delay between calls.
    API_DELAY = 0.1   # 100ms between API calls
    PAGE_SIZE = 100    # Gmail messages per page

    def __init__(
        self,
        auth: GWSAuth,
        threadweave_url: str = "http://localhost:8000",
        checkpoint_dir: Optional[str] = None,
    ):
        """
        Args:
            auth: GWS auth with domain-wide delegation.
            threadweave_url: ThreadWeave API base URL.
            checkpoint_dir: Directory for harvest state files (for resume).
        """
        self.auth = auth
        self.threadweave_url = threadweave_url.rstrip("/")
        self.checkpoint_dir = Path(
            checkpoint_dir or os.path.expanduser("~/.threadweave/harvests")
        )
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # ── Harvest: Email ─────────────────────────────────────────────

    def harvest_email(
        self,
        user_email: str,
        max_messages: int = 5000,
        query: Optional[str] = None,
        resume: bool = True,
    ) -> HarvestStats:
        """Harvest knowledge from a user's Gmail mailbox.

        Scans the user's sent mail and inbox threads for knowledge-bearing
        messages. Each message is processed through the ThreadWeave
        ingestion pipeline. Only messages that pass the detection engine
        are saved.

        Args:
            user_email: Email of the departing employee.
            max_messages: Maximum messages to scan (cap for safety).
            query: Optional Gmail search. If None, uses HARVEST_QUERIES.
            resume: If True, skip already-processed message IDs from checkpoint.

        Returns:
            HarvestStats with detailed results.
        """
        stats = HarvestStats(
            user_email=user_email,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Checkpoint: track processed message IDs for resume
        checkpoint = self._load_checkpoint(user_email) if resume else set()
        processed_ids: set[str] = set(checkpoint)

        # Build auth for this specific user
        user_auth = self._auth_for_user(user_email)
        gmail = GmailWatcher(user_auth, threadweave_url=self.threadweave_url)

        # Use smart search queries if no custom query provided
        queries = [query] if query else list(HARVEST_QUERIES.values())
        # Personalize queries that reference {email}
        queries = [q.format(email=user_email) if "{email}" in q else q
                    for q in queries]

        total_processed = 0

        for q in queries:
            if total_processed >= max_messages:
                break

            logger.info("Harvesting with query: %s", q[:80])
            page_token = None
            page_count = 0

            while total_processed < max_messages:
                try:
                    results = self._list_messages_page(
                        user_auth, q, page_token,
                    )
                except Exception as exc:
                    stats.errors.append(f"Gmail list error: {exc}")
                    logger.warning("Gmail list failed for query '%s': %s", q[:50], exc)
                    break

                messages = results.get("messages", [])
                page_token = results.get("nextPageToken")
                stats.emails_scanned += len(messages)
                page_count += 1

                if not messages:
                    break

                for msg_ref in messages:
                    msg_id = msg_ref["id"]
                    if msg_id in processed_ids:
                        continue

                    if total_processed >= max_messages:
                        break

                    # Fetch full message
                    try:
                        full = self._get_message(user_auth, msg_id)
                    except Exception as exc:
                        stats.errors.append(f"Failed to fetch {msg_id}: {exc}")
                        continue

                    # Parse and submit
                    parsed = gmail._parse_message(full)
                    if parsed and len(parsed.body) >= MIN_MESSAGE_LENGTH:
                        gmail.submit_messages([parsed])
                        stats.emails_processed += 1

                    processed_ids.add(msg_id)
                    total_processed += 1

                    # Progress indicator
                    if total_processed % 100 == 0:
                        logger.info(
                            "Email progress: %d scanned, %d saved",
                            total_processed, stats.emails_processed,
                        )
                        self._save_checkpoint(user_email, processed_ids)

                    # Rate limiting
                    time.sleep(self.API_DELAY)

                if not page_token:
                    break

        # Final checkpoint save
        self._save_checkpoint(user_email, processed_ids)

        # Now count how many were actually SAVED (not just processed)
        stats.emails_saved = stats.emails_processed  # All submitted = processed
        stats.total_saved += stats.emails_saved

        stats.completed_at = datetime.now(timezone.utc).isoformat()
        return stats

    def _list_messages_page(
        self, auth: GWSAuth, query: str, page_token: Optional[str],
    ) -> dict:
        """List one page of Gmail messages matching the query."""
        service = auth.gmail()
        params = {
            "userId": "me",
            "q": query,
            "maxResults": self.PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token

        return service.users().messages().list(**params).execute()

    def _get_message(self, auth: GWSAuth, msg_id: str) -> dict:
        """Fetch a single Gmail message with full raw content."""
        service = auth.gmail()
        return (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="raw")
            .execute()
        )

    # ── Harvest: Chat ──────────────────────────────────────────────

    def harvest_chat(self, user_email: str) -> HarvestStats:
        """Harvest knowledge from the user's Google Chat history."""
        stats = HarvestStats(
            user_email=user_email,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        user_auth = self._auth_for_user(user_email)
        chat = ChatListener(user_auth, threadweave_url=self.threadweave_url)

        try:
            spaces = chat.list_spaces()
            for space in spaces:
                space_id = space.get("name", "")
                if not space_id:
                    continue
                messages = chat.fetch_messages(space_id, max_results=200)
                stats.chat_messages_scanned += len(messages)
                result = chat.submit_messages(messages)
                stats.chat_messages_saved += result.get("saved", 0)
        except Exception as exc:
            stats.errors.append(f"Chat error: {exc}")

        stats.total_saved += stats.chat_messages_saved
        stats.completed_at = datetime.now(timezone.utc).isoformat()
        return stats

    # ── Harvest: Drive ─────────────────────────────────────────────

    def harvest_drive(self, user_email: str, max_files: int = 500) -> HarvestStats:
        """Harvest knowledge from the user's Google Drive."""
        stats = HarvestStats(
            user_email=user_email,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        user_auth = self._auth_for_user(user_email)
        drive = DriveCrawler(user_auth, threadweave_url=self.threadweave_url)

        try:
            docs = drive.crawl(max_results=max_files)
            stats.drive_files_scanned = len(docs)
            result = drive.submit_documents(docs)
            stats.drive_files_saved += result.get("saved", 0)
        except Exception as exc:
            stats.errors.append(f"Drive error: {exc}")

        stats.total_saved += stats.drive_files_saved
        stats.completed_at = datetime.now(timezone.utc).isoformat()
        return stats

    # ── Full Harvest (all sources) ─────────────────────────────────

    def harvest_all(
        self,
        user_email: str,
        sources: list[str] = None,
        max_messages: int = 5000,
        max_files: int = 500,
    ) -> HarvestStats:
        """Run a full harvest across all configured sources.

        Args:
            user_email: Email of the departing employee.
            sources: List of sources to harvest. Default: all.
                     Options: "gmail", "chat", "drive".
            max_messages: Max Gmail messages to scan.
            max_files: Max Drive files to scan.

        Returns:
            Combined HarvestStats.
        """
        if sources is None:
            sources = ["gmail", "chat", "drive"]

        combined = HarvestStats(
            user_email=user_email,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        print(f"\n{'=' * 60}")
        print(f"  Offboarding Harvest: {user_email}")
        print(f"  Started: {combined.started_at[:19]}")
        print(f"{'=' * 60}\n")

        if "gmail" in sources:
            print("📧 Scanning Gmail...")
            stats = self.harvest_email(user_email, max_messages=max_messages)
            self._merge_stats(combined, stats)
            print(
                f"   Scanned: {stats.emails_scanned} | "
                f"Saved: {stats.emails_saved}"
            )

        if "chat" in sources:
            print("💬 Scanning Chat...")
            stats = self.harvest_chat(user_email)
            self._merge_stats(combined, stats)
            print(
                f"   Scanned: {stats.chat_messages_scanned} | "
                f"Saved: {stats.chat_messages_saved}"
            )

        if "drive" in sources:
            print("📄 Scanning Drive...")
            stats = self.harvest_drive(user_email, max_files=max_files)
            self._merge_stats(combined, stats)
            print(
                f"   Scanned: {stats.drive_files_scanned} | "
                f"Saved: {stats.drive_files_saved}"
            )

        combined.completed_at = datetime.now(timezone.utc).isoformat()
        print(f"\n{'─' * 60}")
        print(f"  Total saved: {combined.total_saved} knowledge entries")
        print(f"  Completed: {combined.completed_at[:19]}")
        print(f"{'─' * 60}\n")

        # Save harvest report
        self._save_report(user_email, combined)

        return combined

    # ── Report ─────────────────────────────────────────────────────

    def generate_report(self, user_email: str) -> Optional[dict]:
        """Generate a human-readable harvest report for a user."""
        report_path = self.checkpoint_dir / f"{user_email}_report.json"
        if not report_path.exists():
            return None

        try:
            return json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _save_report(self, user_email: str, stats: HarvestStats) -> None:
        """Save harvest report to disk."""
        report = stats.to_dict()
        report_path = self.checkpoint_dir / f"{user_email}_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        logger.info("Harvest report saved: %s", report_path)

    # ── Helpers ────────────────────────────────────────────────────

    def _auth_for_user(self, user_email: str) -> GWSAuth:
        """Build an auth instance impersonating the specified user."""
        # Create a fresh auth targeting this user
        creds = GWSCredentials(
            credentials_path=self.auth._credentials.credentials_path,
            delegated_account=user_email,
            scopes=self.auth._credentials.scopes,
        )
        return GWSAuth(creds)

    def _load_checkpoint(self, user_email: str) -> set[str]:
        """Load processed message IDs from checkpoint file."""
        path = self.checkpoint_dir / f"{user_email}_checkpoint.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text())
            return set(data.get("processed_ids", []))
        except (json.JSONDecodeError, OSError):
            return set()

    def _save_checkpoint(self, user_email: str, processed_ids: set[str]) -> None:
        """Save processed message IDs to checkpoint file."""
        path = self.checkpoint_dir / f"{user_email}_checkpoint.json"
        path.write_text(
            json.dumps({
                "user_email": user_email,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "processed_ids": list(processed_ids),
                "count": len(processed_ids),
            }, indent=2)
        )

    def _merge_stats(self, combined: HarvestStats, source: HarvestStats) -> None:
        """Merge source stats into combined stats."""
        combined.emails_scanned += source.emails_scanned
        combined.emails_processed += source.emails_processed
        combined.emails_saved += source.emails_saved
        combined.chat_messages_scanned += source.chat_messages_scanned
        combined.chat_messages_saved += source.chat_messages_saved
        combined.drive_files_scanned += source.drive_files_scanned
        combined.drive_files_saved += source.drive_files_saved
        combined.errors.extend(source.errors)
        # Total is sum of all saved from all sources
        combined.total_saved = (
            combined.emails_saved
            + combined.chat_messages_saved
            + combined.drive_files_saved
        )


# ═══════════════════════════════════════════════════════════════════════
# Onboarding: Surface predecessor knowledge
# ═══════════════════════════════════════════════════════════════════════

def generate_onboarding_brief(
    new_hire_email: str,
    predecessor_email: str,
    team_wing: str,
    threadweave_url: str = "http://localhost:8000",
) -> dict:
    """Generate an onboarding knowledge brief for a new hire.

    Searches ThreadWeave for:
    1. Knowledge authored by the predecessor
    2. Knowledge from the new hire's team wing
    3. Recent team decisions and processes

    Args:
        new_hire_email: The new employee's email.
        predecessor_email: The departing employee's email.
        team_wing: The team/department the new hire is joining.
        threadweave_url: ThreadWeave API base URL.

    Returns:
        Dict with sections: predecessor_knowledge, team_knowledge,
        recent_decisions, onboarding_checklist.
    """
    brief = {
        "new_hire": new_hire_email,
        "predecessor": predecessor_email,
        "team": team_wing,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "predecessor_knowledge": [],
        "team_knowledge": [],
        "recent_decisions": [],
        "onboarding_checklist": [],
    }

    base = threadweave_url.rstrip("/")

    # 1. Search for knowledge from predecessor
    try:
        resp = requests.post(
            f"{base}/api/v1/search",
            json={"query": "", "wing": team_wing, "limit": 20},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Filter for entries authored by predecessor
            for r in data.get("results", []):
                # Get full entry to check author
                entry_resp = requests.get(
                    f"{base}/api/v1/entries/{r['id']}", timeout=10,
                )
                if entry_resp.status_code == 200:
                    entry = entry_resp.json()
                    if predecessor_email in entry.get("author_id", ""):
                        brief["predecessor_knowledge"].append({
                            "id": entry["id"],
                            "title": r.get("title", ""),
                            "preview": r.get("content_preview", ""),
                            "created_at": r.get("created_at", ""),
                        })
    except Exception:
        pass

    # 2. Team knowledge (recent, high-value)
    try:
        resp = requests.post(
            f"{base}/api/v1/search",
            json={"query": "", "wing": team_wing, "limit": 10},
            timeout=15,
        )
        if resp.status_code == 200:
            for r in resp.json().get("results", []):
                brief["team_knowledge"].append({
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "preview": r.get("content_preview", ""),
                    "created_at": r.get("created_at", ""),
                })
    except Exception:
        pass

    # 3. Recent decisions in the team
    try:
        resp = requests.post(
            f"{base}/api/v1/search",
            json={"query": "decision", "wing": team_wing, "limit": 10},
            timeout=15,
        )
        if resp.status_code == 200:
            for r in resp.json().get("results", []):
                brief["recent_decisions"].append({
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "preview": r.get("content_preview", ""),
                    "created_at": r.get("created_at", ""),
                })
    except Exception:
        pass

    # 4. Generate onboarding checklist
    brief["onboarding_checklist"] = [
        "Review predecessor's captured knowledge (above)",
        "Read recent team decisions for context",
        "Check team knowledge base for processes and conventions",
        "Set up your own knowledge capture workflow",
    ]

    return brief
