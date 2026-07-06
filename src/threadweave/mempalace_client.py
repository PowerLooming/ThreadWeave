"""
MemPalace client — wraps MemPalace MCP tools for programmatic access.

Two modes:
1. Direct Python API (imports mempalace modules)
2. MCP subprocess (calls mempalace-mcp via stdio)

The Python API mode is used for tight integration; MCP mode for
decoupled deployment where the MCP server runs as a separate process.
"""

import json
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchResult:
    drawer_id: str
    wing: str
    room: str
    content: str
    distance: float
    created_at: str = ""


class MemPalaceClient:
    """Client for MemPalace operations.

    Uses MCP subprocess mode by default for deployment flexibility.
    For direct Python API access, set use_direct_api=True and ensure
    mempalace is installed in the same environment.
    """

    def __init__(
        self,
        palace_path: str = "~/.mempalace/palace",
        use_direct_api: bool = False,
    ):
        self.palace_path = palace_path
        self.use_direct_api = use_direct_api

    def search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Semantic search across the palace."""
        if self.use_direct_api:
            return self._search_direct(query, wing, room, limit)
        return self._search_mcp(query, wing, room, limit)

    def add_drawer(
        self,
        content: str,
        wing: str,
        room: str,
        title: str = "",
        source: str = "threadweave",
    ) -> str:
        """Add a verbatim drawer to the palace. Returns drawer ID."""
        if self.use_direct_api:
            return self._add_drawer_direct(content, wing, room, title, source)
        return self._add_drawer_mcp(content, wing, room, title, source)

    def list_wings(self) -> list[dict]:
        """List all wings."""
        pass

    def get_taxonomy(self) -> dict:
        """Get full wing → room → count tree."""
        pass

    def _search_direct(
        self, query: str, wing: Optional[str], room: Optional[str], limit: int
    ) -> list[SearchResult]:
        """Direct Python API for search."""
        try:
            from mempalace.searcher import search_memories
            from mempalace.config import MempalaceConfig

            config = MempalaceConfig()
            results = search_memories(
                query=query,
                config=config,
                wing=wing,
                room=room,
                limit=limit,
            )
            return [
                SearchResult(
                    drawer_id=r.get("id", ""),
                    wing=r.get("wing", ""),
                    room=r.get("room", ""),
                    content=r.get("content", ""),
                    distance=r.get("distance", 0.5),
                )
                for r in results
            ]
        except ImportError:
            raise RuntimeError(
                "Direct API mode requires mempalace installed. "
                "Use MCP mode or install: pip install mempalace"
            )

    def _search_mcp(
        self, query: str, wing: Optional[str], room: Optional[str], limit: int
    ) -> list[SearchResult]:
        """MCP subprocess mode for search."""
        # Call mempalace-mcp via stdio JSON-RPC
        # Placeholder — implement with actual MCP client
        return []

    def _add_drawer_direct(
        self, content: str, wing: str, room: str, title: str, source: str
    ) -> str:
        """Direct Python API for adding a drawer."""
        try:
            from mempalace.palace import get_collection
            from mempalace.config import MempalaceConfig

            config = MempalaceConfig()
            collection = get_collection(config)
            # collection.add(...) — implementation depends on MemPalace API
            return "drawer_id_placeholder"
        except ImportError:
            raise RuntimeError("Direct API mode requires mempalace installed")

    def _add_drawer_mcp(
        self, content: str, wing: str, room: str, title: str, source: str
    ) -> str:
        """MCP subprocess mode for adding a drawer."""
        # Placeholder — implement with actual MCP client
        return "drawer_id_placeholder"
