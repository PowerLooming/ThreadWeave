# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
MemPalace client — wraps MemPalace for programmatic access.

Two modes:
1. Direct Python API (imports mempalace modules) — for tight integration
2. CLI subprocess (calls `mempalace search` via subprocess) — decoupled fallback

The direct API uses MemPalace's hybrid search (BM25 + vector cosine)
and delegates drawer storage to ChromaDB through MemPalace's collection layer.
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("threadweave.mempalace")


@dataclass
class SearchResult:
    """A single search result from MemPalace."""
    drawer_id: str
    wing: str
    room: str
    content: str
    distance: float          # Raw distance from vector store (lower = closer)
    similarity: float = 0.0  # Normalized 0-1 similarity
    bm25_score: float = 0.0  # BM25 lexical score
    source_file: str = ""
    created_at: str = ""


class MemPalaceClient:
    """Client for MemPalace search and storage operations.

    Uses direct Python API by default. Falls back to CLI subprocess
    when the direct import fails (e.g., missing chromadb deps).

    Palace path defaults to ~/.mempalace/palace/default unless
    MEMPALACE_PALACE_PATH is set in the environment.
    """

    def __init__(
        self,
        palace_path: Optional[str] = None,
    ):
        self.palace_path = palace_path or os.environ.get(
            "MEMPALACE_PALACE_PATH",
            os.path.expanduser("~/.mempalace/palace/default"),
        )
        self._direct_available: Optional[bool] = None

    # ── capability check ──────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Check if MemPalace direct API is importable."""
        if self._direct_available is None:
            try:
                import mempalace.searcher  # noqa: F401
                self._direct_available = True
            except ImportError:
                self._direct_available = False
        return self._direct_available

    # ── search ─────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Semantic+BM25 hybrid search across the palace.

        Uses MemPalace's hybrid ranking (60% vector + 40% BM25 by default).
        Falls back to CLI subprocess if direct API is unavailable.

        Returns empty list on any error — callers should always have
        a keyword fallback.
        """
        if self.available:
            try:
                return self._search_direct(query, wing, room, limit)
            except Exception as exc:
                logger.warning(
                    "MemPalace direct search failed: %s — falling back to CLI", exc
                )
        return self._search_cli(query, wing, room, limit)

    def _search_direct(
        self, query: str, wing: Optional[str], room: Optional[str], limit: int
    ) -> list[SearchResult]:
        """Direct Python API — MemPalace hybrid search."""
        from mempalace.searcher import (
            search as mp_search,
            _distance_to_similarity,
            _metric_for_collection,
        )
        from mempalace.palace import get_collection

        # MemPalace search() uses n_results (not limit) and prints to stdout.
        # We need the raw collection query for programmatic use.
        col = get_collection(self.palace_path, collection_name="mempalace_drawers")
        if col is None:
            logger.warning("No MemPalace collection found at %s", self.palace_path)
            return []

        from mempalace.searcher import build_where_filter

        where = build_where_filter(wing=wing, room=room)
        metric = _metric_for_collection(col)

        kwargs = {
            "query_texts": [query],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        chroma_result = col.query(**kwargs)

        # Extract results from ChromaDB response
        ids_list = chroma_result.get("ids", [[]])
        docs_list = chroma_result.get("documents", [[]])
        metas_list = chroma_result.get("metadatas", [[]])
        dists_list = chroma_result.get("distances", [[]])

        ids = ids_list[0] if ids_list else []
        docs = docs_list[0] if docs_list else []
        metas = metas_list[0] if metas_list else []
        dists = dists_list[0] if dists_list else []

        if not ids:
            return []

        # Build results with vector similarity
        results = []
        for i, (did, doc, meta, dist) in enumerate(
            zip(ids, docs, metas, dists)
        ):
            meta = meta or {}
            sim = _distance_to_similarity(dist, metric)
            results.append(SearchResult(
                drawer_id=did,
                wing=meta.get("wing", ""),
                room=meta.get("room", ""),
                content=doc or "",
                distance=float(dist) if dist is not None else 0.0,
                similarity=round(sim, 4),
                bm25_score=0.0,  # Not available from raw Chroma query
                source_file=meta.get("source_file", ""),
                created_at=meta.get("created_at", ""),
            ))

        # Apply BM25 re-ranking (mirrors MemPalace's _hybrid_rank)
        from mempalace.searcher import _bm25_scores, _hybrid_rank

        hits = [
            {"text": r.content, "distance": r.distance, "metadata": {
                "wing": r.wing, "room": r.room, "source_file": r.source_file,
                "created_at": r.created_at,
            }}
            for r in results
        ]
        hits = _hybrid_rank(hits, query, metric=metric)

        # Map back to SearchResult with BM25 scores
        ranked = []
        for hit in hits:
            sim = _distance_to_similarity(hit["distance"], metric)
            ranked.append(SearchResult(
                drawer_id=hit.get("metadata", {}).get("id", ""),
                wing=hit.get("metadata", {}).get("wing", ""),
                room=hit.get("metadata", {}).get("room", ""),
                content=hit["text"],
                distance=float(hit.get("distance", 0)),
                similarity=round(sim, 4),
                bm25_score=round(hit.get("bm25_score", 0), 3),
                source_file=hit.get("metadata", {}).get("source_file", ""),
                created_at=hit.get("metadata", {}).get("created_at", ""),
            ))
        return ranked

    def _search_cli(
        self, query: str, wing: Optional[str], room: Optional[str], limit: int
    ) -> list[SearchResult]:
        """CLI fallback — calls `mempalace search` as a subprocess."""
        try:
            # Use `mempalace search` CLI which outputs formatted text.
            # For programmatic use, we prefer the direct API; this is a fallback.
            cmd = ["mempalace", "search", query, "--n-results", str(limit)]
            if wing:
                cmd.extend(["--wing", wing])
            if room:
                cmd.extend(["--room", room])
            env = {**os.environ, "MEMPALACE_PALACE_PATH": self.palace_path}
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, env=env,
            )
            if result.returncode != 0:
                logger.warning("mempalace CLI search failed: %s", result.stderr)
                return []
            # Parse CLI output (simplistic — match the formatted output)
            return self._parse_cli_output(result.stdout)
        except FileNotFoundError:
            logger.warning("mempalace CLI not found on PATH")
            return []
        except Exception as exc:
            logger.warning("mempalace CLI search error: %s", exc)
            return []

    def _parse_cli_output(self, stdout: str) -> list[SearchResult]:
        """Parse the formatted output from `mempalace search`."""
        results = []
        current = {}
        in_content = False
        content_lines = []

        for line in stdout.split("\n"):
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped[:4]:
                # New result header: "[N] wing / room"
                if current and content_lines:
                    current["content"] = "\n".join(content_lines)
                    results.append(self._dict_to_result(current))
                current = {}
                content_lines = []
                in_content = False
                # Parse wing/room: "[1] engineering / database"
                bracket_end = stripped.index("]")
                rest = stripped[bracket_end + 1:].strip()
                if " / " in rest:
                    wing, room = rest.split(" / ", 1)
                    current["wing"] = wing.strip()
                    current["room"] = room.strip()
            elif stripped.startswith("Match:"):
                # "Match:  cosine_sim=0.85  bm25=1.2"
                parts = stripped.replace("Match:", "").strip().split()
                for part in parts:
                    if "=" in part:
                        key, val = part.split("=", 1)
                        try:
                            current[key] = float(val)
                        except ValueError:
                            pass
            elif in_content:
                content_lines.append(stripped)
            elif stripped == "" and current:
                in_content = True

        # Don't forget the last result
        if current and content_lines:
            current["content"] = "\n".join(content_lines)
            results.append(self._dict_to_result(current))

        return results

    def _dict_to_result(self, d: dict) -> SearchResult:
        sim = d.get("cosine_sim", d.get("similarity", 0.0))
        return SearchResult(
            drawer_id=d.get("id", ""),
            wing=d.get("wing", ""),
            room=d.get("room", ""),
            content=d.get("content", ""),
            distance=1.0 - float(sim) if sim else 1.0,
            similarity=float(sim),
            bm25_score=float(d.get("bm25", 0)),
            source_file=d.get("source_file", ""),
            created_at=d.get("created_at", ""),
        )

    # ── storage ────────────────────────────────────────────────────

    def add_drawer(
        self,
        content: str,
        wing: str,
        room: str,
        title: str = "",
        source: str = "threadweave",
        created_at: str = "",
        author_id: str = "",
        content_type: str = "",
    ) -> Optional[str]:
        """Add a verbatim drawer to the MemPalace collection.

        Returns the ChromaDB document ID on success, None on failure.
        """
        if not self.available:
            return None

        try:
            from mempalace.palace import get_collection
            import uuid

            col = get_collection(self.palace_path, collection_name="mempalace_drawers")
            if col is None:
                return None

            drawer_id = str(uuid.uuid4())[:8]
            metadata = {
                "wing": wing,
                "room": room,
                "source_file": source,
                "title": title,
                "created_at": created_at,
                "author_id": author_id,
                "content_type": content_type,
            }
            # Remove empty values so ChromaDB doesn't choke
            metadata = {k: v for k, v in metadata.items() if v}

            col.add(
                ids=[drawer_id],
                documents=[content],
                metadatas=[metadata],
            )
            logger.debug("Added drawer %s to MemPalace", drawer_id)
            return drawer_id
        except Exception as exc:
            logger.warning("Failed to add drawer to MemPalace: %s", exc)
            return None

    # ── utility ────────────────────────────────────────────────────

    def list_wings(self) -> list[dict]:
        """List all wings with entry counts."""
        if not self.available:
            return []
        try:
            from mempalace.palace import get_collection
            col = get_collection(self.palace_path, collection_name="mempalace_drawers")
            if col is None:
                return []
            result = col.get(include=["metadatas"])
            wing_counts: dict[str, int] = {}
            for meta in (result.get("metadatas", []) or []):
                wing = (meta or {}).get("wing", "unknown")
                wing_counts[wing] = wing_counts.get(wing, 0) + 1
            return [
                {"name": name, "entry_count": count}
                for name, count in sorted(wing_counts.items())
            ]
        except Exception as exc:
            logger.warning("list_wings failed: %s", exc)
            return []
