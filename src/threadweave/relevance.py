# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Relevance engine — joins semantic search with org proximity and temporal validity.

Sits between MemPalace's semantic search and the access layer.
Re-ranks results based on:
1. Semantic similarity (from MemPalace)
2. Org proximity (from OrgModel) — same team at time of knowledge?
3. Freshness — has the knowledge been superseded or flagged as stale?
4. Authority — was this written by a domain expert?
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class RelevanceScore:
    """Breakdown of relevance scoring for a single result."""
    semantic: float = 0.0       # 0.0-1.0, from vector search
    org_proximity: float = 0.0  # 0.0-1.0, same team at time = 1.0
    freshness: float = 0.0      # 0.0-1.0, brand new = 1.0, years old = low
    authority: float = 0.0      # 0.0-1.0, domain expert = high
    combined: float = 0.0       # Weighted combination


@dataclass
class RankedResult:
    """A search result with relevance scoring."""
    drawer_id: str
    wing: str
    room: str
    content_preview: str      # First 200 chars
    created_at: str           # ISO datetime
    author_team: str          # Team that produced this knowledge
    author_role: str          # Role of the author
    relevance: RelevanceScore = field(default_factory=RelevanceScore)


# ── Scoring weights ───────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "semantic": 0.40,
    "org_proximity": 0.30,
    "freshness": 0.20,
    "authority": 0.10,
}


class RelevanceEngine:
    """Re-ranks MemPalace search results with org context."""

    def __init__(
        self,
        org_model=None,  # OrgModel instance
        weights: Optional[dict] = None,
        staleness_days: int = 365,
    ):
        self.org_model = org_model
        self.weights = weights or DEFAULT_WEIGHTS
        self.staleness_days = staleness_days

    def rank(
        self,
        search_results: list[dict],
        requester_context: Optional[dict] = None,
        semantic_scores: Optional[dict[str, float]] = None,
    ) -> list[RankedResult]:
        """Re-rank search results based on org context.

        Args:
            search_results: Raw results from MemPalace semantic search.
                Each dict should have: id, wing, room, content, created_at,
                and optionally: distance, similarity, bm25_score.
            requester_context: {"team": "...", "role": "...", "person_id": "..."}
            semantic_scores: Optional pre-computed semantic scores keyed by
                drawer ID. When provided (from MemPalace hybrid search),
                these override the distance-based computation.

        Returns:
            Ranked results with full relevance breakdown.
        """
        ranked = []
        for result in search_results:
            score = self._score_result(result, requester_context, semantic_scores)
            ranked.append(RankedResult(
                drawer_id=result.get("id", ""),
                wing=result.get("wing", ""),
                room=result.get("room", ""),
                content_preview=str(result.get("content", ""))[:200],
                created_at=result.get("created_at", ""),
                author_team=result.get("wing", ""),
                author_role=result.get("author_role", "unknown"),
                relevance=score,
            ))

        ranked.sort(key=lambda r: r.relevance.combined, reverse=True)
        return ranked

    def _score_result(
        self, result: dict, requester: Optional[dict],
        semantic_scores: Optional[dict[str, float]] = None,
    ) -> RelevanceScore:
        """Compute full relevance breakdown for one result."""
        score = RelevanceScore()

        # 1. Semantic similarity — prefer pre-computed MemPalace hybrid score
        drawer_id = result.get("id", "")
        if semantic_scores and drawer_id in semantic_scores:
            score.semantic = semantic_scores[drawer_id]
        elif "similarity" in result:
            score.semantic = float(result["similarity"])
        else:
            score.semantic = self._normalize_semantic(result.get("distance", 0.5))

        # 2. Org proximity
        score.org_proximity = self._compute_org_proximity(result, requester)

        # 3. Freshness
        score.freshness = self._compute_freshness(result.get("created_at", ""))

        # 4. Authority
        score.authority = self._compute_authority(result)

        # Combine with weights
        score.combined = (
            score.semantic * self.weights["semantic"]
            + score.org_proximity * self.weights["org_proximity"]
            + score.freshness * self.weights["freshness"]
            + score.authority * self.weights["authority"]
        )

        return score

    def _normalize_semantic(self, distance: float) -> float:
        """Convert distance (lower=better) to similarity (higher=better)."""
        # MemPalace returns cosine distance (0-2). Convert to 0-1 similarity.
        return max(0.0, 1.0 - (distance / 2.0))

    def _compute_org_proximity(
        self, result: dict, requester: Optional[dict]
    ) -> float:
        """Compute how close the knowledge source is to the requester's org context."""
        if not requester or not self.org_model:
            return 0.5  # Neutral when no org context

        requester_team = requester.get("team", "")
        knowledge_team = result.get("wing", "")

        if knowledge_team == requester_team:
            return 1.0

        # Future: query org model for reporting chain proximity, team adjacency
        return 0.3

    def _compute_freshness(self, created_at: str) -> float:
        """Score based on how recently the knowledge was created/verified."""
        if not created_at:
            return 0.5

        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now() - created.replace(tzinfo=None)).days
            if age_days < 0:
                return 1.0
            # Exponential decay: brand new = 1.0, staleness_days = 0.37
            return max(0.1, pow(0.5, age_days / self.staleness_days))
        except (ValueError, TypeError):
            return 0.5

    def _compute_authority(self, result: dict) -> float:
        """Score based on author's domain expertise."""
        role = result.get("author_role", "unknown")
        auth_scores = {
            "tech_lead": 1.0,
            "architect": 1.0,
            "senior": 0.8,
            "domain_expert": 0.9,
            "mid": 0.5,
            "junior": 0.3,
            "unknown": 0.5,
        }
        return auth_scores.get(role, 0.5)

    def detect_stale_knowledge(self, results: list[dict]) -> list[dict]:
        """Flag knowledge that may be outdated.

        Returns list of results whose freshness is below threshold,
        with a recommendation to review.
        """
        stale = []
        for result in results:
            freshness = self._compute_freshness(result.get("created_at", ""))
            if freshness < 0.3:  # Below 30% freshness
                result["stale_warning"] = (
                    f"Knowledge is {freshness:.0%} fresh. "
                    f"Consider verifying."
                )
                stale.append(result)
        return stale


def push_route(
    new_knowledge: dict,
    org_model,
    relevance: RelevanceEngine,
) -> list[str]:
    """Determine which teams should be notified of new knowledge.

    Args:
        new_knowledge: Newly saved drawer metadata
        org_model: OrgModel for team adjacency queries
        relevance: RelevanceEngine for scoring

    Returns:
        List of team IDs that should receive a notification.
    """
    source_team = new_knowledge.get("wing", "")
    if not source_team:
        return []

    notify_teams = []

    # Always notify the source team itself
    notify_teams.append(source_team)

    # Find cross-team tunnels in the palace graph
    # Future: query MemPalace tunnels for cross-referenced rooms
    # tunnels = org_model.find_connected_teams(source_team)

    return list(set(notify_teams))
