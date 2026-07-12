# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
BaseConnector — shared interface for all ThreadWeave connectors.

Connectors (Teams, SharePoint, Email) extend this class.
They handle auth + data fetching, then submit to the central
ingestion pipeline via POST /api/v1/ingest.

Common functionality:
    - HTTP client to ThreadWeave API
    - Content hashing for connector-side dedup
    - Standard metadata formatting
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ConnectorResult:
    """Result from submitting content to the ingestion pipeline."""
    entry_id: str
    source: str
    should_save: bool
    content_type: str
    confidence: float
    deduplicated: bool = False
    error: str = ""


class BaseConnector(ABC):
    """
    Abstract base for all ThreadWeave connectors.

    Connectors authenticate with their platform (Teams, SharePoint, Email),
    fetch content, and submit it to the central ingestion pipeline.

    The pipeline handles: dedup, detection, PII filtering, storage.
    """

    def __init__(
        self,
        api_base_url: str = "http://localhost:8000",
        tenant_id: str = "default",
        connector_name: str = "base",
    ):
        self.api_base_url = api_base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.connector_name = connector_name
        self._local_dedup: set[str] = set()
        self.stats = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}

    @abstractmethod
    async def authenticate(self) -> bool:
        """Authenticate with the source platform. Return True on success."""
        ...

    @abstractmethod
    async def fetch_content(self) -> list[dict]:
        """
        Fetch content from the source platform.

        Returns list of dicts with keys:
            content: str    — the text to process
            metadata: dict  — source-specific metadata
        """
        ...

    async def submit(self, content: str, metadata: dict | None = None) -> ConnectorResult:
        """
        Submit content to the central ingestion pipeline.

        Args:
            content: The text content to process
            metadata: Source-specific metadata (sender, channel, site_id, etc.)

        Returns:
            ConnectorResult with processing outcome
        """
        import httpx

        self.stats["submitted"] += 1

        # Local dedup check
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if content_hash in self._local_dedup:
            self.stats["skipped"] += 1
            return ConnectorResult(
                entry_id="", source=self.connector_name,
                should_save=False, content_type="chat",
                confidence=1.0, deduplicated=True,
            )
        self._local_dedup.add(content_hash)

        metadata = metadata or {}
        metadata.setdefault("source_timestamp", datetime.now(timezone.utc).isoformat())

        payload = {
            "content": content,
            "source": self.connector_name,
            "tenant_id": self.tenant_id,
            "metadata": metadata,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.api_base_url}/api/v1/ingest",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("should_save"):
                    self.stats["saved"] += 1
                else:
                    self.stats["skipped"] += 1

                return ConnectorResult(
                    entry_id=data.get("id", ""),
                    source=self.connector_name,
                    should_save=data.get("should_save", False),
                    content_type=data.get("content_type", "unknown"),
                    confidence=data.get("confidence", 0.0),
                    deduplicated=data.get("deduplicated", False),
                )

        except Exception as e:
            self.stats["errors"] += 1
            logger.error("Ingest submission failed: %s", e)
            return ConnectorResult(
                entry_id="", source=self.connector_name,
                should_save=False, content_type="error",
                confidence=0.0, error=str(e),
            )

    async def health_check(self) -> dict:
        """Check if the ThreadWeave API is reachable."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.api_base_url}/api/v1/health")
                return resp.json()
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}
