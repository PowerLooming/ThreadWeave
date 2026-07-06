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
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


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
        """Initialize org model with optional MemPalace KnowledgeGraph path."""
        self._kg = None
        self._kg_path = kg_path
        self._entities: dict[str, OrgEntity] = {}

    def add_entity(self, entity_id: str, name: str, entity_type: str) -> OrgEntity:
        """Register an entity in the org model."""
        entity = OrgEntity(id=entity_id, name=name, entity_type=entity_type)
        self._entities[entity_id] = entity
        return entity

    def add_relationship(
        self,
        source: str,
        relation: str,
        target: str,
        valid_from: str,
        valid_to: Optional[str] = None,
    ) -> OrgRelationship:
        """Add a relationship between entities with temporal validity."""
        if relation not in RELATION_TYPES:
            raise ValueError(f"Unknown relation type: {relation}")

        rel = OrgRelationship(
            source=source,
            relation=relation,
            target=target,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        return rel

    def get_team(self, person_id: str, as_of: Optional[str] = None) -> Optional[str]:
        """Get the team a person belongs to at a given point in time.

        Args:
            person_id: Person entity ID
            as_of: Point in time to query (ISO date). None = current.
        """
        # In-memory fallback (replace with MemPalace KG in production)
        if person_id in self._entities:
            # Return the entity type if it's a team, or look up membership
            entity = self._entities[person_id]
            if entity.entity_type == "team":
                return entity.name
        return None

    def get_team_members(self, team_id: str, as_of: Optional[str] = None) -> list[str]:
        """Get all members of a team at a given point in time."""
        raise NotImplementedError("Requires MemPalace KG integration")

    def get_chain_of_command(
        self, entity_id: str, as_of: Optional[str] = None
    ) -> list[str]:
        """Get the reporting chain for an entity (person → manager → director → ...)."""
        raise NotImplementedError("Requires MemPalace KG integration")

    def find_relevant_people(
        self, teams_or_domains: list[str], as_of: Optional[str] = None
    ) -> list[str]:
        """Find people with knowledge relevant to given teams or domains."""
        raise NotImplementedError("Requires MemPalace KG integration")

    def export_to_mempalace_kg(self) -> list[dict]:
        """Export the org model to MemPalace knowledge graph triples.

        Returns a list of triple dicts ready for mempalace_kg_add.
        """
        triples = []
        for entity_id, entity in self._entities.items():
            triples.append({
                "subject": entity_id,
                "relation": "is_a",
                "object": entity.entity_type,
                "valid_from": "2000-01-01",
            })
        return triples

    def sync_from_hr_system(self, hris_data: list[dict]) -> int:
        """Bulk-load org structure from HRIS data.

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

    # Check if knowledge team was once the same as requester team
    # at the time the knowledge was created
    # (For now, simple heuristic)
    return 0.2  # Placeholder — full implementation queries KG
