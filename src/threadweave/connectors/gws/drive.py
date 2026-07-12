# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Google Drive Crawler — extracts knowledge from Google Workspace documents.

Crawls Google Drive for documents, extracts text content, and submits
to ThreadWeave ingestion pipeline.

Supported formats:
    - Google Docs → export as text/plain
    - Google Sheets → export as CSV (first sheet)
    - Google Slides → export as text/plain
    - PDF → download + PyMuPDF extraction
    - Plain text / Markdown files → direct read

Uses Drive API v3: files.list, files.export, files.get

Design:
    - Poll-based with change tracking
    - Skips binary files that can't be extracted
    - Respects file ownership (only processes files the service account can access)
    - Maps Drive folder structure → ThreadWeave wings/rooms
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from threadweave.connectors.gws.auth import GWSAuth

logger = logging.getLogger("threadweave.gws.drive")

# MIME types we can extract text from
SUPPORTED_GOOGLE_MIMES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

SUPPORTED_FILE_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml",
    ".yaml", ".yml", ".log", ".py", ".js", ".ts", ".html",
    ".css", ".sql", ".rst",
}

# Max file size to process (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


class DriveCrawler:
    """Crawl Google Drive for knowledge-bearing documents."""

    # Google Doc MIME types that we export natively
    def __init__(
        self,
        auth: GWSAuth,
        threadweave_url: str = "http://localhost:8000",
        folder_mapping: Optional[dict[str, str]] = None,
    ):
        """
        Args:
            auth: GWS authentication instance.
            threadweave_url: ThreadWeave API base URL.
            folder_mapping: Optional mapping of Drive folder IDs → wing names.
        """
        self.auth = auth
        self.threadweave_url = threadweave_url.rstrip("/")
        self.folder_mapping = folder_mapping or {}
        self._processed_ids: set[str] = set()
        self._processed_hashes: set[str] = set()

    # ── Crawl ─────────────────────────────────────────────────────

    def crawl(
        self, query: str = "", max_results: int = 50,
    ) -> list[dict]:
        """Crawl Drive for documents and extract text.

        Args:
            query: Drive search query (e.g. 'modifiedTime > "2025-01-01"').
            max_results: Max files to process.

        Returns:
            List of extracted document dicts: {name, mime_type, text, folder, url}.
        """
        service = self.auth.drive()
        files = self._list_files(service, query, max_results)
        documents = []

        for f in files:
            doc = self._process_file(service, f)
            if doc:
                documents.append(doc)

        return documents

    def _list_files(self, service, query: str, max_results: int) -> list[dict]:
        """List files in Drive matching the query."""
        search = query or "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        try:
            results = (
                service.files()
                .list(
                    q=search,
                    pageSize=max_results,
                    fields="files(id, name, mimeType, size, modifiedTime, parents, webViewLink)",
                )
                .execute()
            )
            return results.get("files", [])
        except Exception as exc:
            logger.error("Drive list failed: %s", exc)
            return []

    def _process_file(self, service, file_info: dict) -> Optional[dict]:
        """Process a single file: extract text, skip if unprocessable."""
        file_id = file_info.get("id", "")
        file_name = file_info.get("name", "unknown")
        mime_type = file_info.get("mimeType", "")
        file_size = int(file_info.get("size", 0))

        # Skip already processed
        if file_id in self._processed_ids:
            return None

        # Skip large files
        if file_size > MAX_FILE_SIZE:
            return None

        # Extract text
        text = self._extract_text(service, file_id, file_name, mime_type)
        if not text or len(text.strip()) < 50:
            # Too short to be meaningful knowledge
            self._processed_ids.add(file_id)
            return None

        # Dedup by content hash
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        if content_hash in self._processed_hashes:
            self._processed_ids.add(file_id)
            return None
        self._processed_hashes.add(content_hash)

        self._processed_ids.add(file_id)

        # Determine wing from folder mapping
        parents = file_info.get("parents", [])
        wing = "drive"
        for parent_id in parents:
            if parent_id in self.folder_mapping:
                wing = self.folder_mapping[parent_id]
                break

        modified = file_info.get("modifiedTime", "")

        return {
            "id": file_id,
            "name": file_name,
            "mime_type": mime_type,
            "text": text,
            "folder": parents[0] if parents else "",
            "wing": wing,
            "url": file_info.get("webViewLink", ""),
            "modified": modified,
        }

    def _extract_text(
        self, service, file_id: str, file_name: str, mime_type: str,
    ) -> str:
        """Extract text from a file based on its MIME type."""
        # Google Docs → export
        if mime_type in SUPPORTED_GOOGLE_MIMES:
            export_mime = SUPPORTED_GOOGLE_MIMES[mime_type]
            return self._export_google_doc(service, file_id, export_mime)

        # Text files → download directly
        ext = Path(file_name).suffix.lower()
        if ext in SUPPORTED_FILE_EXTENSIONS or mime_type.startswith("text/"):
            return self._download_text_file(service, file_id)

        # PDF → try PyMuPDF
        if mime_type == "application/pdf" or ext == ".pdf":
            return self._extract_pdf(service, file_id)

        return ""

    def _export_google_doc(
        self, service, file_id: str, export_mime: str,
    ) -> str:
        """Export a Google Doc/Sheet/Slide as text."""
        try:
            data = service.files().export(
                fileId=file_id, mimeType=export_mime,
            ).execute()
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            return str(data)
        except Exception as exc:
            logger.debug("Export failed for %s: %s", file_id, exc)
            return ""

    def _download_text_file(self, service, file_id: str) -> str:
        """Download a text file from Drive."""
        try:
            data = service.files().get_media(fileId=file_id).execute()
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            return str(data)
        except Exception as exc:
            logger.debug("Download failed for %s: %s", file_id, exc)
            return ""

    def _extract_pdf(self, service, file_id: str) -> str:
        """Extract text from a PDF using PyMuPDF if available."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.debug("PyMuPDF not available — skipping PDF %s", file_id)
            return ""

        try:
            data = service.files().get_media(fileId=file_id).execute()
            doc = fitz.open(stream=data, filetype="pdf")
            pages = []
            for page in doc:
                pages.append(page.get_text())
            doc.close()
            return "\n".join(pages)
        except Exception as exc:
            logger.debug("PDF extraction failed for %s: %s", file_id, exc)
            return ""

    # ── Submit ────────────────────────────────────────────────────

    def submit_documents(self, documents: list[dict]) -> dict:
        """Submit extracted documents to ThreadWeave."""
        stats = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}

        for doc in documents:
            stats["submitted"] += 1
            try:
                resp = requests.post(
                    f"{self.threadweave_url}/api/v1/ingest",
                    json={
                        "content": (
                            f"Document: {doc['name']}\n"
                            f"Source: Google Drive ({doc.get('url', '')})\n\n"
                            f"{doc['text']}"
                        ),
                        "source": "google_drive",
                        "metadata": {
                            "file_id": doc["id"],
                            "file_name": doc["name"],
                            "mime_type": doc["mime_type"],
                            "url": doc.get("url", ""),
                            "modified": doc.get("modified", ""),
                            "wing": doc.get("wing", "drive"),
                            "room": "documents",
                            "title": doc["name"],
                        },
                    },
                    timeout=60,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    if data.get("should_save"):
                        stats["saved"] += 1
                    else:
                        stats["skipped"] += 1
                else:
                    stats["errors"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("Failed to submit document %s: %s", doc["name"], exc)

        return stats

    def process_drive(self, query: str = "") -> dict:
        """Crawl Drive and submit found documents.

        One-shot operation.

        Args:
            query: Optional Drive search query.

        Returns:
            Processing stats.
        """
        documents = self.crawl(query=query)
        return self.submit_documents(documents)
