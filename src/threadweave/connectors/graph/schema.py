# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Graph Schema — maps ThreadWeave knowledge entries to Microsoft Graph external items.

Microsoft Graph external connection items require:
- id: unique identifier (max 128 chars)
- properties: key-value pairs matching the connection schema
- content: the searchable text content
- acl: access control list entries

This module defines the ThreadWeave → Graph mapping and the connection schema
that must be registered with Microsoft Graph before syncing items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── Connection schema (registered once via POST /external/connections) ──

CONNECTION_ID = "threadweave"
CONNECTION_NAME = "ThreadWeave Organizational Memory"
CONNECTION_DESCRIPTION = (
    "Organizational knowledge captured from emails, Slack, Teams, "
    "and manual entries. Includes decisions, architectural rationale, "
    "process documentation, and tribal knowledge."
)

# Schema definition — must be registered before items can be synced.
# Maps ThreadWeave fields to Microsoft Graph external item properties.
# Properties marked isSearchable=True, isRetrievable=True appear in Copilot.
GRAPH_SCHEMA = {
    "name": CONNECTION_ID,
    "description": CONNECTION_DESCRIPTION,
    "id": CONNECTION_ID,
    "searchSettings": {
        "searchResultTemplates": [
            {
                "id": "threadweave-result",
                "priority": 1,
                "layout": {
                    "type": "resultType",
                    "properties": [
                        {"name": "title", "type": "string", "isSearchable": True},
                        {"name": "wing", "type": "string", "isRefinable": True},
                        {"name": "room", "type": "string", "isRefinable": True},
                        {"name": "contentType", "type": "string", "isRefinable": True},
                        {"name": "author", "type": "string"},
                        {"name": "createdDateTime", "type": "dateTime"},
                    ],
                },
            }
        ]
    },
}


# ── Property definitions (the searchable fields) ──

@dataclass
class GraphItemProperties:
    """Properties of an external item pushed to Microsoft Graph.

    These become searchable/filterable in Microsoft Search and Copilot.
    """

    # ── Required / always present ──
    title: str = ""               # Entry title or auto-generated summary
    content: str = ""             # Full verbatim text — the searchable body

    # ── Org context (refinable in search) ──
    wing: str = ""                # Team/department (refinable facet)
    room: str = ""                # Topic (refinable facet)
    contentType: str = ""         # answer, decision, question, reference (refinable)

    # ── Metadata ──
    author: str = ""              # Who wrote it
    authorTeam: str = ""          # Team at time of writing
    createdDateTime: str = ""     # ISO 8601, when the knowledge was captured
    sourceType: str = ""          # email, slack, teams, manual, api
    scope: str = "team"           # team, department, organization

    # ── Link back to ThreadWeave ──
    url: str = ""                 # Deep link to the entry in ThreadWeave

    def to_graph_properties(self) -> dict[str, Any]:
        """Convert to the format expected by Microsoft Graph API."""
        return {
            "title": self.title,
            "wing": self.wing,
            "room": self.room,
            "contentType": self.contentType,
            "author": self.author,
            "authorTeam": self.authorTeam,
            "createdDateTime": (
                self.createdDateTime
                if self.createdDateTime
                else datetime.now(timezone.utc).isoformat()
            ),
            "sourceType": self.sourceType,
            "scope": self.scope,
            "url": self.url,
        }


# ── ACL (Access Control List) ──

@dataclass
class GraphAclEntry:
    """A single ACL entry on an external item.

    Access types: grant (allow) or deny.
    Types: user (by Entra ID object ID), group (by Entra ID group ID),
           everyone (tenant-wide), everyoneExceptGuests.
    """
    accessType: str = "grant"     # grant or deny
    type: str = "group"           # user, group, everyone, everyoneExceptGuests
    value: str = ""               # Entra ID object/group ID, or empty for everyone

    def to_dict(self) -> dict:
        return {
            "accessType": self.accessType,
            "type": self.type,
            "value": self.value,
        }


# ── External Item (the payload sent to Graph) ──

@dataclass
class GraphExternalItem:
    """Complete external item ready for Microsoft Graph upload."""

    item_id: str                            # Unique ID (maps to ThreadWeave entry ID)
    properties: GraphItemProperties         # Searchable properties
    content: str                            # Full text content
    acl: list[GraphAclEntry] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Convert to the JSON payload for PUT /external/connections/{id}/items/{id}."""
        payload = {
            "id": self.item_id,
            "properties": self.properties.to_graph_properties(),
            "content": {
                "type": "text",
                "value": self.content,
            },
        }
        if self.acl:
            payload["acl"] = [a.to_dict() for a in self.acl]
        return payload


# ── Mapping: ThreadWeave entry → Graph external item ──

def map_threadweave_to_graph(
    entry: dict,
    base_url: str = "http://localhost:8000",
    wing_to_group: Optional[dict[str, str]] = None,
) -> GraphExternalItem:
    """Convert a ThreadWeave entry dict to a Graph external item.

    Args:
        entry: ThreadWeave entry dict (from _memory_store or API)
        base_url: Base URL for ThreadWeave (used for source links)
        wing_to_group: Optional mapping of wing name → Entra ID group ID
                       for ACLs. If not provided, uses "everyone" (tenant-wide).

    Returns:
        GraphExternalItem ready for upload.
    """
    entry_id = entry.get("id", "")
    wing = entry.get("wing", "")
    room = entry.get("room", "")

    # Build ACL: grant access to the wing's M365 group
    acl_entries = []
    if wing_to_group and wing in wing_to_group:
        acl_entries.append(GraphAclEntry(
            accessType="grant",
            type="group",
            value=wing_to_group[wing],
        ))
    else:
        # Default: visible to everyone in the tenant
        acl_entries.append(GraphAclEntry(
            accessType="grant",
            type="everyone",
            value="",
        ))

    return GraphExternalItem(
        item_id=entry_id,
        properties=GraphItemProperties(
            title=entry.get("title", "") or _auto_title(entry.get("content", "")),
            content=entry.get("content", ""),
            wing=wing,
            room=room,
            contentType=entry.get("content_type", "unknown"),
            author=entry.get("author_id", "unknown"),
            authorTeam=wing,
            createdDateTime=entry.get("created_at", ""),
            sourceType=entry.get("source_type", "manual"),
            scope=entry.get("scope", "team"),
            url=f"{base_url.rstrip('/')}/api/v1/entries/{entry_id}",
        ),
        content=entry.get("content", ""),
        acl=acl_entries,
    )


def _auto_title(content: str, max_length: int = 100) -> str:
    """Generate a title from content when none is provided."""
    if not content:
        return "Untitled entry"
    # Use first sentence or truncate
    first_sentence = content.split(".")[0].strip()
    if len(first_sentence) > max_length:
        return first_sentence[: max_length - 3] + "..."
    return first_sentence
