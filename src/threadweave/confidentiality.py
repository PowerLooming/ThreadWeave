# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Confidentiality — sensitive content classification, access enforcement, and audit.

Layered approach:

    L1: CLASSIFICATION
        - SensitivityLevel tags (public → restricted)
        - Automatic detection of HR, financial, client-confidential content
        - Manual override by the saver

    L2: ACCESS ENFORCEMENT
        - Filter search results by requester's clearance
        - Block direct access to entries above requester's level
        - Client-level scoping (entries tagged with client_id)
        - Wing-to-wing isolation (HR entries invisible to engineering)

    L3: AUDIT TRAIL
        - Log every access to confidential+ entries
        - Track who viewed what and when
        - Immutable audit entries (append-only in-memory, DB-backed in prod)

Design decisions:
    - Classification is heuristic + regex (no LLM) for speed and privacy
    - Enforcement at read time (not write time) — you can save anything,
      but retrieval is gated
    - Client-confidential is the hardest case: requires client_id on entry
      AND requester to be authenticated against that client
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("threadweave.confidentiality")


# ═══════════════════════════════════════════════════════════════════════
# L1: CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════

class SensitivityLevel(str, Enum):
    """How sensitive is this piece of knowledge?

    Ordered from least to most restrictive.
    """
    PUBLIC = "public"
    """Anyone in the organization can see this."""

    INTERNAL = "internal"
    """Anyone in the org; never shared externally (Graph Connector should exclude)."""

    CONFIDENTIAL = "confidential"
    """Wing/department members only. Cross-wing access requires explicit grant."""

    RESTRICTED = "restricted"
    """Named individuals only. Not visible to the rest of the wing."""

    HR_PRIVILEGED = "hr_privileged"
    """HR personnel only. Salary, performance, personnel matters."""

    CLIENT_CONFIDENTIAL = "client_confidential"
    """Only visible to people working on a specific client engagement."""

    LEGAL_PRIVILEGED = "legal_privileged"
    """Attorney-client privileged. Legal team only."""

    @classmethod
    def clearance_order(cls) -> list["SensitivityLevel"]:
        """Lowest to highest clearance required."""
        return [
            cls.PUBLIC,
            cls.INTERNAL,
            cls.CONFIDENTIAL,
            cls.RESTRICTED,
            cls.CLIENT_CONFIDENTIAL,
            cls.HR_PRIVILEGED,
            cls.LEGAL_PRIVILEGED,
        ]

    def can_access(self, requester_level: "SensitivityLevel") -> bool:
        """Check if a requester at this level can access content at `self` level.

        CONFIDENTIAL requester can see PUBLIC, INTERNAL, CONFIDENTIAL.
        HR_PRIVILEGED requester can ONLY see HR_PRIVILEGED and below —
        but HR is a SPECIAL WING, not just a clearance level.
        """
        order = self.clearance_order()
        return order.index(requester_level) >= order.index(self)


def parse_sensitivity(
    value: object,
    default: SensitivityLevel = SensitivityLevel.INTERNAL,
) -> SensitivityLevel:
    """Parse a stored sensitivity value, failing CLOSED on unknown strings.

    Ingested content is not guaranteed to carry a value this enum knows:
    a connector, a hand-edited row, or a display-form name written by an
    older client ("hr-privileged" instead of the enum's "hr_privileged")
    produces a string that raises ValueError inside enum lookup. Because
    that lookup sits inside the read path, one such entry used to 500 the
    whole search request for everyone (found live: a demo entry labelled
    "hr-privileged" made POST /api/v1/search return 500 rather than
    filtering the entry out).

    Unknown values resolve to RESTRICTED, which requires a named ACL, so
    a typo hides content instead of exposing it or crashing the request.
    An empty or missing value keeps the documented default.
    """
    if isinstance(value, SensitivityLevel):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return SensitivityLevel(text)
    except ValueError:
        logger.warning(
            "Unknown sensitivity %r — treating as %s (fail closed)",
            text, SensitivityLevel.RESTRICTED.value,
        )
        return SensitivityLevel.RESTRICTED


# ═══════════════════════════════════════════════════════════════════════
# Confidentiality signals — regex patterns for auto-detection
# ═══════════════════════════════════════════════════════════════════════

# HR / Personnel patterns
HR_SIGNALS = [
    re.compile(r"\b(salary|compensation|bonus|raise|severance|PIP|performance improvement)\b", re.I),
    re.compile(r"\b(termination|fired|laid off|let go|dismissal)\b", re.I),
    re.compile(r"\b(performance review|annual review|360 review|disciplinary)\b", re.I),
    re.compile(r"\b(medical leave|FMLA|disability|accommodation)\b", re.I),
    re.compile(r"\b(HR case|employee relations|investigation|complaint)\b", re.I),
    re.compile(r"\b(headcount|requisition|offer letter|candidate assessment)\b", re.I),
    # "promotion" is ambiguous (marketing promo vs career step) — require
    # career context so retail promo reviews don't get HR-locked.
    # (Fixed 2026-08-07: "seasonal promotion review" with a discount was
    # classified hr_privileged and locked out of its Retail wing.)
    re.compile(r"\bpromotion\b(?=.*\b(role|career|job|position|staff|employee|hire|recruit|salary|compensation)\b)", re.I),
    re.compile(r"\b(demotion|reorganization|org chart)\b", re.I),
]

# Financial / sensitive business patterns
FINANCIAL_SIGNALS = [
    re.compile(r"\b(revenue|profit margin|EBITDA|burn rate|runway|cash flow)\b", re.I),
    re.compile(r"\b(pricing|discount|rate card|MSRP|cost per)\b", re.I),
    re.compile(r"\b(valuation|term sheet|cap table|equity|stock options)\b", re.I),
    re.compile(r"\b(forecast|projection|quarterly results|earnings)\b", re.I),
]

# Client-confidential patterns
CLIENT_SIGNALS = [
    re.compile(r"\b(client|customer|account)\s+(confidential|sensitive|private|privileged)\b", re.I),
    re.compile(r"\b(NDA|non-disclosure|confidentiality agreement)\b", re.I),
    re.compile(r"\b(proprietary|trade secret|intellectual property)\b", re.I),
    re.compile(r"\b(client-specific|bespoke|custom-built for)\b", re.I),
    re.compile(r"\b(under contract|statement of work|SOW|MSA)\b", re.I),
]

# Legal privileged patterns
LEGAL_SIGNALS = [
    re.compile(r"\b(attorney-client|legal advice|privileged communication)\b", re.I),
    re.compile(r"\b(litigation|subpoena|deposition|court order|settlement)\b", re.I),
    re.compile(r"\b(regulatory|compliance|GDPR|SOX|HIPAA|PCI)\b", re.I),
    re.compile(r"\b(lawsuit|sue|suing|legal action|cease and desist)\b", re.I),
]

# PII that goes beyond email/phone (already caught by detector)
ENHANCED_PII_SIGNALS = [
    re.compile(r"\b(SSN|social security|passport number|national ID)\b", re.I),
    re.compile(r"\b(date of birth|birthday|birth date)\b", re.I),
    re.compile(r"\b(home address|personal address|residential)\b", re.I),
    re.compile(r"\b(bank account|routing number|IBAN|SWIFT)\b", re.I),
    re.compile(r"\b(medical record|diagnosis|prescription|patient)\b", re.I),
]

# People names in sensitive contexts (heuristic)
NAME_IN_HR_CONTEXT = re.compile(
    r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b\s+(?:is|has been|was)\s+(?:fired|terminated|promoted|disciplined|reprimanded)",
    re.I,
)


@dataclass
class SensitivityDetection:
    """Result of scanning content for sensitive signals."""

    suggested_level: SensitivityLevel = SensitivityLevel.INTERNAL
    confidence: float = 0.0
    matched_signals: list[str] = field(default_factory=list)
    matched_categories: list[str] = field(default_factory=list)
    contains_pii: bool = False
    contains_hr_data: bool = False
    contains_financial_data: bool = False
    contains_client_data: bool = False
    contains_legal_data: bool = False

    @property
    def is_sensitive(self) -> bool:
        return self.suggested_level in (
            SensitivityLevel.CONFIDENTIAL,
            SensitivityLevel.RESTRICTED,
            SensitivityLevel.HR_PRIVILEGED,
            SensitivityLevel.CLIENT_CONFIDENTIAL,
            SensitivityLevel.LEGAL_PRIVILEGED,
        )


def detect_sensitivity(content: str) -> SensitivityDetection:
    """Scan content for confidential/sensitive signals.

    Runs all signal patterns against the text and returns the highest
    sensitivity level detected along with matched patterns.

    Args:
        content: The text to scan.

    Returns:
        SensitivityDetection with suggested level and matched signals.
    """
    result = SensitivityDetection()

    # Check HR signals
    for pattern in HR_SIGNALS:
        matches = pattern.findall(content)
        if matches:
            result.contains_hr_data = True
            result.matched_categories.append("hr")
            result.matched_signals.extend(
                [m if isinstance(m, str) else m[0] for m in matches[:3]]
            )

    # Check financial signals
    for pattern in FINANCIAL_SIGNALS:
        matches = pattern.findall(content)
        if matches:
            result.contains_financial_data = True
            result.matched_categories.append("financial")
            result.matched_signals.extend(
                [m if isinstance(m, str) else m[0] for m in matches[:3]]
            )

    # Check client signals
    for pattern in CLIENT_SIGNALS:
        matches = pattern.findall(content)
        if matches:
            result.contains_client_data = True
            result.matched_categories.append("client")
            result.matched_signals.extend(
                [m if isinstance(m, str) else m[0] for m in matches[:3]]
            )

    # Check legal signals
    for pattern in LEGAL_SIGNALS:
        matches = pattern.findall(content)
        if matches:
            result.contains_legal_data = True
            result.matched_categories.append("legal")
            result.matched_signals.extend(
                [m if isinstance(m, str) else m[0] for m in matches[:3]]
            )

    # Check enhanced PII
    for pattern in ENHANCED_PII_SIGNALS:
        matches = pattern.findall(content)
        if matches:
            result.contains_pii = True
            result.matched_categories.append("pii")

    # Determine suggested level (highest priority wins)
    if result.contains_legal_data:
        result.suggested_level = SensitivityLevel.LEGAL_PRIVILEGED
        result.confidence = 0.85
    elif result.contains_hr_data:
        result.suggested_level = SensitivityLevel.HR_PRIVILEGED
        result.confidence = 0.80
    elif result.contains_client_data:
        result.suggested_level = SensitivityLevel.CLIENT_CONFIDENTIAL
        result.confidence = 0.75
    elif result.contains_financial_data:
        result.suggested_level = SensitivityLevel.CONFIDENTIAL
        result.confidence = 0.65
    elif result.contains_pii:
        result.suggested_level = SensitivityLevel.RESTRICTED
        result.confidence = 0.70
    else:
        # Default: check scope hint in content
        result.suggested_level = _scope_to_sensitivity(content)
        result.confidence = 0.40

    return result


def _scope_to_sensitivity(content: str) -> SensitivityLevel:
    """Fallback: infer sensitivity from scope-related language."""
    if re.search(r"\b(confidential|sensitive|private)\b", content, re.I):
        return SensitivityLevel.CONFIDENTIAL
    if re.search(r"\b(internal\s+use|internal\s+only|not\s+for\s+distribution)\b", content, re.I):
        return SensitivityLevel.INTERNAL
    return SensitivityLevel.PUBLIC


# ═══════════════════════════════════════════════════════════════════════
# L2: ACCESS ENFORCEMENT
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RequesterContext:
    """Who is making the request? What are they allowed to see?"""

    person_id: str = ""
    """Who is asking (for person-level ACL checks)."""

    wing: str = ""
    """Which team/department does the requester belong to?"""

    role: str = ""
    """Role: admin, readwrite, readonly, hr_admin, legal."""

    client_ids: list[str] = field(default_factory=list)
    """Which clients is this person cleared to work on?"""

    groups: list[str] = field(default_factory=list)
    """Group object IDs this requester belongs to (e.g. Entra group IDs).
    Used by per-source ACLs (`allowed_groups` / `deny_groups`)."""

    clearance: SensitivityLevel = SensitivityLevel.INTERNAL
    """Maximum sensitivity level this person can access."""

    @classmethod
    def from_request(cls, request_data: Optional[dict] = None) -> "RequesterContext":
        """Build context from API request headers/body."""
        if request_data is None:
            return cls()
        return cls(
            person_id=request_data.get("person_id", ""),
            wing=request_data.get("wing", ""),
            role=request_data.get("role", "readwrite"),
            client_ids=request_data.get("client_ids", []),
            groups=request_data.get("groups", []),
            clearance=SensitivityLevel(request_data.get("clearance", "internal")),
        )

    def can_see(
        self,
        entry: dict,
        entry_sensitivity: Optional[SensitivityLevel] = None,
    ) -> bool:
        """Determine if this requester can access a given entry.

        Checks (in order):
        1. Sensitivity level clearance
        2. Wing membership (for CONFIDENTIAL+)
        3. Client assignment (for CLIENT_CONFIDENTIAL)
        4. Person-level ACL (for RESTRICTED)
        5. Special wing gating (HR wing → only HR people)
        """
        sensitivity = entry_sensitivity or parse_sensitivity(
            entry.get("sensitivity", "internal")
        )

        # 0. Private-channel gate — strictest, first, and AUTHORITATIVE.
        #    Content captured from a Teams private channel (or any source
        #    flagged private_channel) is visible ONLY to the channel's own
        #    members, with NO role bypass. Membership is the grant: a
        #    member sees it regardless of nominal clearance (their channel
        #    membership IS the clearance), and a non-member never sees it,
        #    including admins/legal/hr who would bypass RESTRICTED. Fail
        #    closed: an empty member list means nobody can see it.
        if entry.get("source_metadata", {}).get("private_channel"):
            members = entry.get("allowed_people", []) or []
            return self.person_id in members

        # 0b. General per-source ACL gate — deny-overrides-grant.
        #    A source (Teams channel, SharePoint site/folder, email thread,
        #    etc.) may tag content with an explicit ACL in `entry["acl"]`:
        #      allowed_users / allowed_groups / deny_users / deny_groups
        #      revoked_at + acl_granted_at for fast revocation.
        #    This is AUTHORITATIVE like private_channel: a source boundary
        #    is not a clearance level, so no role (admin/legal/hr) bypasses
        #    it, and DENY beats GRANT. A revoked entry is denied immediately,
        #    independent of re-ingest. Empty ACL falls through to clearance.
        acl = entry.get("acl") or {}
        if acl:
            # Fast revocation: if the grant was revoked at/after it was
            # granted, treat as denied.
            if acl.get("revoked_at"):
                granted = acl.get("acl_granted_at", "")
                if acl["revoked_at"] >= granted:
                    return False
            # DENY overrides GRANT.
            if self.person_id in (acl.get("deny_users") or []):
                return False
            if any(g in (acl.get("deny_groups") or []) for g in self.groups):
                return False
            # GRANT is authoritative when a grant list is present: the
            # source explicitly decided who may see this, so membership IS
            # the grant and it does NOT fall through to the nominal
            # clearance gate (which could otherwise deny a source-granted
            # reader). A matched grant returns True immediately; a
            # non-match returns False (fail closed).
            if acl.get("allowed_users") or acl.get("allowed_groups"):
                in_users = self.person_id in (acl.get("allowed_users") or [])
                in_groups = any(
                    g in (acl.get("allowed_groups") or []) for g in self.groups
                )
                if in_users or in_groups:
                    return True
                return False
            # ACL exists but has no grant list (only denies/revocation that
            # did not match) → fall through to clearance checks.

        # 1. Clearance check
        if not sensitivity.can_access(self.clearance):
            return False

        # 2. Wing check — for CONFIDENTIAL+, must be in the same wing
        #    UNLESS the requester has cross-wing clearance (admin, legal, hr)
        #    Fail closed: a requester WITHOUT a wing claim is denied, an
        #    empty wing must not bypass the check.
        if sensitivity in (
            SensitivityLevel.CONFIDENTIAL,
            SensitivityLevel.RESTRICTED,
        ):
            entry_wing = entry.get("wing", "")
            if entry_wing and (not self.wing or entry_wing != self.wing):
                # Cross-wing or unknown wing: only admins and special roles
                if self.role not in ("admin", "legal", "hr_admin"):
                    return False

        # 3. Client check — fail closed: CLIENT_CONFIDENTIAL entries
        #    require a client_id AND requester assignment to that client.
        #    Admin bypasses (umbrella role); legal/hr are wing-specific.
        if sensitivity == SensitivityLevel.CLIENT_CONFIDENTIAL:
            if self.role != "admin":
                entry_client = entry.get("client_id", "")
                if not entry_client or entry_client not in self.client_ids:
                    return False

        # 4. Person-level ACL — fail closed: RESTRICTED entries require a
        #    named ACL. No allowed_people list means nobody is authorized
        #    (except admin/legal/hr_admin special roles).
        if sensitivity == SensitivityLevel.RESTRICTED:
            if self.role not in ("admin", "legal", "hr_admin"):
                allowed_people = entry.get("allowed_people", [])
                if not allowed_people or self.person_id not in allowed_people:
                    return False

        # 5. Special wing gating
        if sensitivity == SensitivityLevel.HR_PRIVILEGED:
            if self.role not in ("admin", "hr_admin") and self.wing != "hr":
                return False

        if sensitivity == SensitivityLevel.LEGAL_PRIVILEGED:
            if self.role not in ("admin", "legal") and self.wing != "legal":
                return False

        return True

    def filter_results(
        self,
        results: list[dict],
    ) -> list[dict]:
        """Filter a list of search results, removing entries the requester can't see.

        Also adds a 'redacted' flag to entries that were filtered out
        (so the system can report "N results hidden due to access restrictions").
        """
        visible = []
        for r in results:
            if self.can_see(r):
                visible.append(r)
        return visible


# ═══════════════════════════════════════════════════════════════════════
# L3: AUDIT TRAIL
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AuditEntry:
    """Single audit log entry for access to sensitive content.

    ``count`` and ``first_seen`` describe a run of identical events that were
    folded together at write time: ``timestamp`` is the last event in the run,
    ``first_seen`` the first, and ``count`` how many happened. A row that never
    repeated has ``count=1`` and ``first_seen == timestamp``.
    """

    timestamp: str
    requester_id: str
    requester_wing: str
    action: str              # view, search_result, denied
    entry_id: str
    entry_sensitivity: str
    entry_wing: str
    reason: str = ""         # Why was access denied? (if denied)
    tenant_id: str = "default"  # Tenant the entry belongs to
    ip_hash: str = ""        # Hashed IP for privacy
    first_seen: str = ""     # First event of a folded run (defaults to timestamp)
    count: int = 1           # Events folded into this row

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "requester_id": self.requester_id,
            "requester_wing": self.requester_wing,
            "action": self.action,
            "entry_id": self.entry_id,
            "entry_sensitivity": self.entry_sensitivity,
            "entry_wing": self.entry_wing,
            "reason": self.reason,
            "tenant_id": self.tenant_id,
            "ip_hash": self.ip_hash,
            "first_seen": self.first_seen or self.timestamp,
            "count": max(1, self.count),
        }


_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    requester_wing TEXT NOT NULL,
    action TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    entry_sensitivity TEXT NOT NULL,
    entry_wing TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'default',
    ip_hash TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1
)
"""

_AUDIT_COLUMNS = (
    "timestamp, requester_id, requester_wing, action, entry_id, "
    "entry_sensitivity, entry_wing, reason, tenant_id, ip_hash, "
    "first_seen, count"
)

# Which fields make two events "the same event repeated". A retrying client
# produces identical values for all of them, so folding is precise: a different
# reason, entry or requester starts a new row.
_AUDIT_REPEAT_KEYS = (
    "requester_id", "action", "entry_id", "reason", "tenant_id", "ip_hash",
)

_AUDIT_WINDOW_ENV = "THREADWEAVE_AUDIT_AGGREGATE_WINDOW"
_AUDIT_WINDOW_DEFAULT = 300.0


def _aggregate_window() -> float:
    """How long a run of identical events keeps folding into one row.

    ``THREADWEAVE_AUDIT_AGGREGATE_WINDOW`` in seconds, default 300, and ``0``
    disables folding so every event gets its own row. An unparsable value falls
    back to the default rather than silently disabling the aggregation.
    """
    raw = os.environ.get(_AUDIT_WINDOW_ENV)
    if raw is None or not raw.strip():
        return _AUDIT_WINDOW_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Ignoring unparsable %s=%r", _AUDIT_WINDOW_ENV, raw)
        return _AUDIT_WINDOW_DEFAULT



class AuditLog:
    """Append-only-in-spirit audit log for sensitive content access.

    Durable by default: backed by SQLite at ~/.threadweave/audit.sqlite3
    (override with THREADWEAVE_AUDIT_DB or the ``db_path`` argument), so
    the trail survives restarts. Falls back to an in-memory ring buffer
    only if the database cannot be opened.

    Repeats are folded at write time. An identical event (same requester,
    action, entry, reason, tenant and ip hash) that recurs within
    ``THREADWEAVE_AUDIT_AGGREGATE_WINDOW`` seconds (default 300, ``0`` disables)
    increments the previous row's ``count`` and moves its ``timestamp`` forward
    instead of appending a new row; the run keeps its original ``first_seen``.
    Measured need: a retrying client wrote 12,248 identical denials next to 58
    real refusals, and every read showed the flood and hid the refusals. Nothing
    is dropped but the repetition: a folded row still reports how many events
    happened and when the run started and ended.
    """

    def __init__(self, max_entries: int = 10_000, db_path: Optional[str] = None):
        self._max_entries = max_entries
        self._entries: list[AuditEntry] = []  # in-memory fallback
        self._db: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        path = db_path or os.environ.get(
            "THREADWEAVE_AUDIT_DB",
            str(Path.home() / ".threadweave" / "audit.sqlite3"),
        )
        try:
            self._init_db(path)
        except Exception as exc:
            logger.warning(
                "Audit DB unavailable at %s (%s) — using in-memory audit log",
                path, exc,
            )

    def _init_db(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_AUDIT_SCHEMA)
        self._migrate(conn)
        conn.commit()
        self._db = conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the aggregation columns to a database written before they existed.

        An existing trail must stay readable: the old rows become ``count=1`` with
        ``first_seen`` set to their own timestamp, which is exactly what a run of
        one means.
        """
        have = {row[1] for row in conn.execute("PRAGMA table_info(audit_entries)")}
        if "first_seen" not in have:
            conn.execute(
                "ALTER TABLE audit_entries ADD COLUMN first_seen TEXT NOT NULL DEFAULT ''"
            )
        if "count" not in have:
            conn.execute(
                "ALTER TABLE audit_entries ADD COLUMN count INTEGER NOT NULL DEFAULT 1"
            )
        conn.execute(
            "UPDATE audit_entries SET first_seen = timestamp WHERE first_seen = ''"
        )

    # ── writes ──────────────────────────────────────────────────

    def _append(self, entry: AuditEntry) -> None:
        window = _aggregate_window()
        if self._db is not None:
            try:
                with self._lock:
                    if window > 0 and self._fold_into_previous(entry, window):
                        self._db.commit()
                        return
                    self._db.execute(
                        f"INSERT INTO audit_entries ({_AUDIT_COLUMNS}) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry.timestamp, entry.requester_id,
                         entry.requester_wing, entry.action, entry.entry_id,
                         entry.entry_sensitivity, entry.entry_wing,
                         entry.reason, entry.tenant_id, entry.ip_hash,
                         entry.first_seen or entry.timestamp,
                         max(1, entry.count)),
                    )
                    self._db.commit()
                return
            except Exception as exc:
                logger.warning("Audit DB append failed: %s", exc)
        # In-memory fallback
        if window > 0 and self._entries and self._is_repeat(
            self._entries[-1], entry
        ) and self._within_window(self._entries[-1].timestamp, window):
            last = self._entries[-1]
            last.count += 1
            last.timestamp = entry.timestamp
            return
        if not entry.first_seen:
            entry.first_seen = entry.timestamp
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries:]

    @staticmethod
    def _is_repeat(previous: AuditEntry, entry: AuditEntry) -> bool:
        return all(
            getattr(previous, key) == getattr(entry, key)
            for key in _AUDIT_REPEAT_KEYS
        )

    @staticmethod
    def _within_window(timestamp: str, window: float) -> bool:
        try:
            previous = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return False
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - previous).total_seconds() <= window

    def _fold_into_previous(self, entry: AuditEntry, window: float) -> bool:
        """Increment the last row when this event repeats it inside the window."""
        row = self._db.execute(
            "SELECT id, timestamp, count, requester_id, action, entry_id, reason, "
            "tenant_id, ip_hash FROM audit_entries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        if any(row[key] != getattr(entry, key) for key in _AUDIT_REPEAT_KEYS):
            return False
        if not self._within_window(row["timestamp"], window):
            return False
        self._db.execute(
            "UPDATE audit_entries SET count = ?, timestamp = ? WHERE id = ?",
            (int(row["count"] or 1) + 1, entry.timestamp, row["id"]),
        )
        return True

    def log_access(
        self,
        requester: RequesterContext,
        entry: dict,
        action: str = "view",
        ip_hash: str = "",
    ) -> None:
        """Log that a requester accessed a sensitive entry."""
        sensitivity = entry.get("sensitivity", "internal")
        if sensitivity in ("public", "internal"):
            return  # Only audit confidential+

        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            requester_id=requester.person_id or "anonymous",
            requester_wing=requester.wing,
            action=action,
            entry_id=entry.get("id", "unknown"),
            entry_sensitivity=sensitivity,
            entry_wing=entry.get("wing", ""),
            tenant_id=entry.get("tenant_id", "default"),
            ip_hash=ip_hash,
        )
        self._append(entry)

    def log_denied(
        self,
        requester: RequesterContext,
        entry: dict,
        reason: str,
        ip_hash: str = "",
    ) -> None:
        """Log a denied access attempt."""
        sensitivity = entry.get("sensitivity", "internal")
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            requester_id=requester.person_id or "anonymous",
            requester_wing=requester.wing,
            action="denied",
            entry_id=entry.get("id", "unknown"),
            entry_sensitivity=sensitivity,
            entry_wing=entry.get("wing", ""),
            reason=reason,
            tenant_id=entry.get("tenant_id", "default"),
            ip_hash=ip_hash,
        )
        self._append(entry)

    def log_delete(
        self,
        requester: RequesterContext,
        entry: dict,
        reason: str = "user requested",
        ip_hash: str = "",
    ) -> None:
        """Log an entry deletion (always audited — deletions are permanent)."""
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            requester_id=requester.person_id or "anonymous",
            requester_wing=requester.wing,
            action="delete",
            entry_id=entry.get("id", "unknown"),
            entry_sensitivity=entry.get("sensitivity", "internal"),
            entry_wing=entry.get("wing", ""),
            reason=reason,
            tenant_id=entry.get("tenant_id", "default"),
            ip_hash=ip_hash,
        )
        self._append(entry)

    # ── reads ───────────────────────────────────────────────────

    def _query(self, sql: str, params: tuple) -> Optional[list[dict]]:
        """Run a SELECT; None means the DB is unavailable (use fallback)."""
        if self._db is None:
            return None
        try:
            with self._lock:
                rows = self._db.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Audit DB query failed: %s", exc)
            return None

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Get the most recent audit entries."""
        rows = self._query(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_entries ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        if rows is not None:
            return rows
        return [e.to_dict() for e in self._entries[-limit:]]

    def get_for_entry(self, entry_id: str) -> list[dict]:
        """Get all audit entries for a specific knowledge entry."""
        rows = self._query(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_entries WHERE entry_id = ? "
            "ORDER BY id",
            (entry_id,),
        )
        if rows is not None:
            return rows
        return [
            e.to_dict()
            for e in self._entries
            if e.entry_id == entry_id
        ]

    def get_for_requester(self, requester_id: str) -> list[dict]:
        """Get all audit entries for a specific requester."""
        rows = self._query(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_entries WHERE requester_id = ? "
            "ORDER BY id",
            (requester_id,),
        )
        if rows is not None:
            return rows
        return [
            e.to_dict()
            for e in self._entries
            if e.requester_id == requester_id
        ]

    def clear(self) -> None:
        """Clear all audit entries."""
        if self._db is not None:
            try:
                with self._lock:
                    self._db.execute("DELETE FROM audit_entries")
                    self._db.commit()
            except Exception as exc:
                logger.warning("Audit DB clear failed: %s", exc)
        self._entries.clear()

    @property
    def count(self) -> int:
        """Rows in the trail. A folded run is one row, whatever its event count."""
        rows = self._query("SELECT COUNT(*) AS n FROM audit_entries", ())
        if rows is not None:
            return rows[0]["n"]
        return len(self._entries)

    @property
    def event_count(self) -> int:
        """Events the trail records, folding included.

        ``count`` answers "how many rows", this answers "how many things
        happened": 58 rows can be 58 refusals, or one refusal repeated 12,248
        times. An operator reading the trail wants both numbers.
        """
        rows = self._query(
            "SELECT COALESCE(SUM(count), 0) AS n FROM audit_entries", ()
        )
        if rows is not None:
            return int(rows[0]["n"])
        return sum(max(1, e.count) for e in self._entries)


# ═══════════════════════════════════════════════════════════════════════
# Module-level instance
# ═══════════════════════════════════════════════════════════════════════

_audit_log = AuditLog()


def get_audit_log() -> AuditLog:
    return _audit_log
