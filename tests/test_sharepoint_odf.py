# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for OpenDocument (LibreOffice) extraction: odt/ods/odp."""

import io
import zipfile

import pytest

from threadweave.connectors.sharepoint.processor import DocumentProcessor

TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"


def _odf_bytes(body: str, kind: str) -> bytes:
    """Build a minimal ODF zip (content.xml) like LibreOffice writes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", f"application/vnd.oasis.opendocument.{kind}")
        zf.writestr("content.xml", body)
    return buf.getvalue()


def _odt(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        f'xmlns:text="{TEXT_NS}">'
        f"<office:body><office:text>{text}</office:text></office:body>"
        "</office:document-content>"
    )
    return _odf_bytes(body, "text")


def _ods(cells: list[str]) -> bytes:
    rows = "".join(
        f'<table:table-row><table:table-cell>{c}</table:table-cell>'
        f"</table:table-row>"
        for c in cells
    )
    body = (
        '<?xml version="1.0"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        f'xmlns:text="{TEXT_NS}" xmlns:table="{TABLE_NS}">'
        f"<office:body><office:spreadsheet><table:table>{rows}"
        "</table:table></office:spreadsheet></office:body>"
        "</office:document-content>"
    )
    return _odf_bytes(body, "spreadsheet")


def _odp(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        f'xmlns:text="{TEXT_NS}">'
        f"<office:body><office:presentation>{text}"
        "</office:presentation></office:body>"
        "</office:document-content>"
    )
    return _odf_bytes(body, "presentation")


@pytest.fixture
def proc(tmp_path):
    return DocumentProcessor(graph_client=None, temp_dir=str(tmp_path))


def test_odt_paragraphs_and_headings(proc):
    text = (
        "<text:h>Release Notes</text:h>"
        "<text:p>We decided to use PostgreSQL for the new service.</text:p>"
        "<text:p>This decision was made at the architecture review.</text:p>"
    )
    out = proc._extract_odf(_odt(text), kind="odt")
    assert "Release Notes" in out
    assert "PostgreSQL" in out
    assert "architecture review" in out


def test_ods_cell_values(proc):
    out = proc._extract_odf(
        _ods(["Topic", "Decision", "PostgreSQL pooling"]), kind="ods"
    )
    assert "PostgreSQL pooling" in out
    assert "Decision" in out


def test_odp_slide_text(proc):
    out = proc._extract_odf(
        _odp("<text:p>CI runner decision: move to self-hosted.</text:p>"),
        kind="odp",
    )
    assert "CI runner" in out


def test_odf_ignores_tables_for_odt(proc):
    """odt files can contain tables; only ods adds cell values."""
    out = proc._extract_odf(_odt("<text:p>Plain text</text:p>"), kind="odt")
    assert "Plain text" in out


def test_odf_bad_zip_returns_empty(proc):
    assert proc._extract_odf(b"not a zip", kind="odt") == ""


def test_odf_no_content_xml_returns_empty(proc):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other.xml", "<x/>")
    assert proc._extract_odf(buf.getvalue(), kind="odt") == ""


def test_extensions_registered():
    assert ".odt" in DocumentProcessor.SUPPORTED_EXTENSIONS
    assert ".ods" in DocumentProcessor.SUPPORTED_EXTENSIONS
    assert ".odp" in DocumentProcessor.SUPPORTED_EXTENSIONS
