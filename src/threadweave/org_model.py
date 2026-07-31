# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Org model — temporal organizational structure built on MemPalace knowledge graph.

Uses MemPalace's KnowledgeGraph (SQLite temporal triples) to store:
- Team membership with validity windows
- Reporting lines
- Domain ownership
- Cross-team relationships

Key design decision: The org model IS the knowledge graph. No separate database.
This means org structure changes are just new triples with validity windows,
and queries automatically get temporal correctness.

Dual-mode operation:
  - With kg_path: persists to MemPalace KG + in-memory cache
  - Without kg_path: in-memory only (backward compatible)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("threadweave.org_model")


@dataclass
class OrgEntity:
    """A person, team, or domain in the org model."""
    id: str
    name: str
    entity_type: str  # person, team, domain, role


@dataclass
class OrgRelationship:
    """A relationship between two org entities, valid for a time window."""
    source: str       # entity id
    relation: str     # member_of, reports_to, owns, collaborates_with
    target: str       # entity id
    valid_from: str   # ISO date or datetime
    valid_to: Optional[str] = None  # None = still valid


# ── Known relationship types ──────────────────────────────────────

RELATION_TYPES = {
    "member_of": "Person is a member of a team",
    "reports_to": "Entity reports to another entity",
    "owns": "Team/person owns a domain or system",
    "collaborates_with": "Teams that regularly collaborate",
    "succeeded_by": "Entity was replaced by another (for role transitions)",
    "subteam_of": "Team is a subteam of another team",
}


class OrgModel:
    """Manages org structure as temporal triples in MemPalace knowledge graph.

    Usage:
        model = OrgModel(kg_path="~/.mempalace/knowledge_graph.sqlite3")

        # Add a person
        model.add_entity("harald", "Harald Daltveit", "person")

        # Add team membership with validity
        model.add_relationship("harald", "member_of", "billing_team",
                               valid_from="2020-01-01", valid_to="2024-06-01")
        model.add_relationship("harald", "member_of", "platform_team",
                               valid_from="2024-06-01")

        # Query org at a point in time
        team = model.get_membership("harald", as_of="2022-03-15")
        # -> "billing_team"

        # Query current state
        team = model.get_membership("harald")
        # -> "platform_team"

        # Find all people with relevant knowledge for a topic
        relevant = model.find_relevant_people(["billing_team", "payment_service"])
    """

    def __init__(self, kg_path: Optional[str] = None):
        """Initialize org model with optional MemPalace KnowledgeGraph path.

        Args:
            kg_path: Path to MemPalace KG SQLite database.
                     None (default) → in-memory only.
                     e.g. "~/.mempalace/knowledge_graph.sqlite3"
        """
        self._kg = None
        self._kg_path = kg_path
        self._entities: dict[str, OrgEntity] = {}
        self._relationships: list[OrgRelationship] = []

        if kg_path:
            self._init_kg(kg_path)

    def _init_kg(self, kg_path: str) -> None:
        """Lazily initialize the MemPalace KnowledgeGraph connection."""
        try:
            from mempalace.knowledge_graph import KnowledgeGraph
            import os
            expanded = os.path.expanduser(kg_path)
            self._kg = KnowledgeGraph(db_path=expanded)
            logger.info("OrgModel connected to MemPalace KG: %s", expanded)
        except ImportError:
            logger.warning(
                "MemPalace not installed — org model running in-memory only. "
                "Install with: pip install mempalace"
            )
        except Exception as e:
            logger.warning(
                "Failed to connect to MemPalace KG at %s: %s — "
                "org model running in-memory only", kg_path, e
            )

    # ── Entity management ────────────────────────────────────────

    def add_entity(self, entity_id: str, name: str, entity_type: str) -> OrgEntity:
        """Register an entity in the org model.

        Persists to both in-memory cache and MemPalace KG (if connected).
        """
        entity = OrgEntity(id=entity_id, name=name, entity_type=entity_type)
        self._entities[entity_id] = entity

        if self._kg:
            try:
                self._kg.add_entity(
                    name=entity_id,
                    entity_type=entity_type,
                    properties={"display_name": name},
                )
            except Exception as e:
                logger.warning("Failed to persist entity '%s' to KG: %s", entity_id, e)

        return entity

    # ── Relationship management ──────────────────────────────────

    def add_relationship(
        self,
        source: str,
        relation: str,
        target: str,
        valid_from: str,
        valid_to: Optional[str] = None,
    ) -> OrgRelationship:
        """Add a relationship between entities with temporal validity.

        Persists to both in-memory list and MemPalace KG (if connected).
        """
        if relation not in RELATION_TYPES:
            raise ValueError(f"Unknown relation type: {relation}")

        rel = OrgRelationship(
            source=source,
            relation=relation,
            target=target,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._relationships.append(rel)

        if self._kg:
            try:
                self._kg.add_triple(
                    subject=source,
                    predicate=relation,
                    obj=target,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            except Exception as e:
                logger.warning(
                    "Failed to persist relationship '%s -[%s]-> %s' to KG: %s",
                    source, relation, target, e,
                )

        return rel

    # ── Queries ──────────────────────────────────────────────────

    def get_team(self, person_id: str, as_of: Optional[str] = None) -> Optional[str]:
        """Get the team a person belongs to at a given point in time.

        Queries KG first (if connected), falls back to in-memory relationships.

        Args:
            person_id: Person entity ID
            as_of: Point in time to query (ISO date). None = current.

        Returns:
            Team ID string, or None if not found.
        """
        # Try KG first
        if self._kg:
            try:
                results = self._kg.query_entity(
                    name=person_id,
                    direction="outgoing",
                    as_of=as_of,
                )
                for triple in results:
                    if triple.get("predicate") == "member_of":
                        return triple["obj"]
            except Exception as e:
                logger.warning("KG query for team membership failed: %s", e)

        # Fall back to in-memory
        return self._get_team_in_memory(person_id, as_of)

    def _get_team_in_memory(
        self, person_id: str, as_of: Optional[str] = None
    ) -> Optional[str]:
        """In-memory fallback: find the most recent team membership."""
        if as_of is None:
            as_of = datetime.now().isoformat()[:10]

        best = None
        for rel in self._relationships:
            if rel.source != person_id or rel.relation != "member_of":
                continue
            if rel.valid_from > as_of:
                continue
            if rel.valid_to and rel.valid_to < as_of:
                continue
            if best is None or rel.valid_from > best.valid_from:
                best = rel
        return best.target if best else None

    def get_team_members(
        self, team_id: str, as_of: Optional[str] = None
    ) -> list[str]:
        """Get all members of a team at a given point in time.

        Queries KG first (if connected), falls back to in-memory relationships.

        Args:
            team_id: Team entity ID
            as_of: Point in time to query (ISO date). None = current.

        Returns:
            List of person entity IDs who are members of the team.
        """
        members: set[str] = set()

        # Try KG first
        if self._kg:
            try:
                results = self._kg.query_entity(
                    name=team_id,
                    direction="incoming",
                    as_of=as_of,
                )
                for triple in results:
                    if triple.get("predicate") == "member_of":
                        members.add(triple["subject"])
                if members:
                    return sorted(members)
            except Exception as e:
                logger.warning("KG query for team members failed: %s", e)

        # Fall back to in-memory
        if as_of is None:
            as_of = datetime.now().isoformat()[:10]

        for rel in self._relationships:
            if rel.target != team_id or rel.relation != "member_of":
                continue
            if rel.valid_from > as_of:
                continue
            if rel.valid_to and rel.valid_to < as_of:
                continue
            members.add(rel.source)

        return sorted(members)

    def get_chain_of_command(
        self, entity_id: str, as_of: Optional[str] = None
    ) -> list[str]:
        """Get the reporting chain for an entity (person → manager → director → ...).

        Follows 'reports_to' relations recursively until no manager is found.
        Queries KG first (if connected), falls back to in-memory.

        Args:
            entity_id: Person entity ID to start from
            as_of: Point in time to query (ISO date). None = current.

        Returns:
            List of entity IDs forming the chain [person, manager, director, ...].
        """
        chain = [entity_id]
        seen = {entity_id}

        if as_of is None:
            as_of = datetime.now().isoformat()[:10]

        # Try KG first
        if self._kg:
            try:
                current = entity_id
                while True:
                    results = self._kg.query_entity(
                        name=current,
                        direction="outgoing",
                        as_of=as_of,
                    )
                    manager = None
                    for triple in results:
                        if triple.get("predicate") == "reports_to":
                            manager = triple["obj"]
                            break
                    if manager and manager not in seen:
                        chain.append(manager)
                        seen.add(manager)
                        current = manager
                    else:
                        break
                return chain
            except Exception as e:
                logger.warning("KG query for chain of command failed: %s", e)

        # Fall back to in-memory
        current = entity_id
        while True:
            manager = None
            for rel in self._relationships:
                if rel.source != current or rel.relation != "reports_to":
                    continue
                if rel.valid_from > as_of:
                    continue
                if rel.valid_to and rel.valid_to < as_of:
                    continue
                if manager is None or rel.valid_from > manager.valid_from:
                    manager = rel
            if manager and manager.target not in seen:
                chain.append(manager.target)
                seen.add(manager.target)
                current = manager.target
            else:
                break

        return chain

    def find_relevant_people(
        self, teams_or_domains: list[str], as_of: Optional[str] = None
    ) -> list[str]:
        """Find people with knowledge relevant to given teams or domains.

        For each team/domain, finds:
        - Direct team members (member_of)
        - Domain owners (owns)
        - Collaborating team members (collaborates_with → member_of)

        Queries KG first (if connected), falls back to in-memory.

        Args:
            teams_or_domains: List of team/domain entity IDs
            as_of: Point in time to query (ISO date). None = current.

        Returns:
            Deduplicated list of person entity IDs.
        """
        people: set[str] = set()

        # Try KG first
        if self._kg:
            try:
                for target in teams_or_domains:
                    # Direct members
                    members = self.get_team_members(target, as_of=as_of)
                    people.update(members)

                    # Domain owners
                    results = self._kg.query_entity(
                        name=target,
                        direction="incoming",
                        as_of=as_of,
                    )
                    for triple in results:
                        if triple.get("predicate") == "owns":
                            people.add(triple["subject"])

                    # Collaborating teams → their members
                    collab_results = self._kg.query_entity(
                        name=target,
                        direction="both",
                        as_of=as_of,
                    )
                    for triple in collab_results:
                        if triple.get("predicate") == "collaborates_with":
                            collab_team = (
                                triple["obj"] if triple["subject"] == target
                                else triple["subject"]
                            )
                            collab_members = self.get_team_members(
                                collab_team, as_of=as_of
                            )
                            people.update(collab_members)

                if people:
                    return sorted(people)
            except Exception as e:
                logger.warning("KG query for relevant people failed: %s", e)

        # Fall back to in-memory
        for target in teams_or_domains:
            people.update(self.get_team_members(target, as_of=as_of))

        return sorted(people)

    # ── Export / sync ────────────────────────────────────────────

    def export_to_mempalace_kg(self) -> list[dict]:
        """Export the org model to MemPalace knowledge graph triples.

        Returns a list of triple dicts ready for bulk import.
        Includes both entity declarations and relationships.
        """
        triples = []

        # Entity declarations
        for entity_id, entity in self._entities.items():
            triples.append({
                "subject": entity_id,
                "predicate": "is_a",
                "object": entity.entity_type,
                "valid_from": "2000-01-01",
            })

        # Relationships
        for rel in self._relationships:
            triples.append({
                "subject": rel.source,
                "predicate": rel.relation,
                "object": rel.target,
                "valid_from": rel.valid_from,
                "valid_to": rel.valid_to,
            })

        return triples

    def sync_from_hr_system(self, hris_data: list[dict]) -> int:
        """Bulk-load org structure from HRIS data.

        Persists to both in-memory cache and MemPalace KG (if connected).

        Args:
            hris_data: List of dicts with keys:
                person_id, person_name, team_id, team_name,
                manager_id, valid_from, valid_to

        Returns:
            Number of relationships created.
        """
        count = 0
        for record in hris_data:
            self.add_entity(record["person_id"], record["person_name"], "person")
            self.add_entity(record["team_id"], record["team_name"], "team")
            self.add_relationship(
                record["person_id"], "member_of", record["team_id"],
                valid_from=record.get("valid_from", "2000-01-01"),
                valid_to=record.get("valid_to"),
            )
            if record.get("manager_id"):
                self.add_entity(record["manager_id"], "", "person")
                self.add_relationship(
                    record["person_id"], "reports_to", record["manager_id"],
                    valid_from=record.get("valid_from", "2000-01-01"),
                    valid_to=record.get("valid_to"),
                )
            count += 2
        return count

    def close(self) -> None:
        """Close the MemPalace KG connection if open."""
        if self._kg:
            try:
                self._kg.close()
            except Exception as e:
                logger.warning("Error closing KG connection: %s", e)
            self._kg = None

    def __del__(self) -> None:
        """Clean up KG connection on garbage collection."""
        self.close()

    # ── Stats ────────────────────────────────────────────────────

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def relationship_count(self) -> int:
        return len(self._relationships)

    @property
    def has_kg(self) -> bool:
        return self._kg is not None


def org_proximity_score(
    knowledge_team: str,
    knowledge_time: str,
    requester_team: str,
    org_model: OrgModel,
) -> float:
    """Score how relevant knowledge is based on org proximity.

    Args:
        knowledge_team: Team that produced the knowledge
        knowledge_time: When the knowledge was created (ISO date)
        requester_team: Team requesting the knowledge
        org_model: The org model to query

    Returns:
        Score 0.0-1.0, where 1.0 = same team, 0.0 = unrelated.
    """
    if knowledge_team == requester_team:
        return 1.0

    # Check if teams collaborate
    if org_model.has_kg:
        try:
            results = org_model._kg.query_entity(
                name=knowledge_team,
                direction="both",
                as_of=knowledge_time,
            )
            for triple in results:
                if triple.get("predicate") == "collaborates_with":
                    if triple["obj"] == requester_team or triple["subject"] == requester_team:
                        return 0.7
        except Exception:
            pass

    # Check in-memory relationships
    for rel in org_model._relationships:
        if rel.relation != "collaborates_with":
            continue
        if {rel.source, rel.target} == {knowledge_team, requester_team}:
            if rel.valid_from <= knowledge_time and (
                rel.valid_to is None or rel.valid_to >= knowledge_time
            ):
                return 0.7

    # Fallback: same parent team?
    # Check if knowledge team was once the same as requester team
    # at the time the knowledge was created
    # (For now, simple heuristic)
    return 0.2
