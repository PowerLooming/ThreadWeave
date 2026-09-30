# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Ingest behaviour when the gate reports PII.

The gate's PII verdict used to be destructive: ``rejected_pii`` and the message
was gone. It now redacts the identifiers and keeps the message, because a
calibration fit on hand-labelled real mail found no bar that is both usable and
safe (a bar set for 95% recall still cost 34% of the negatives on the best local
backend), and because a phone number in a signature would otherwise delete every
signed message. ``THREADWEAVE_PII_MODE=reject`` restores the old behaviour.

Detection is mocked so these tests do not need a model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.detector import ContentType, DetectionResult

client = TestClient(app)

BODY = (
    "Vi besluttet å flytte sesjonscachen til Redis fordi lasten ble for høy mot "
    "slutten av dagen, og vi mister sesjoner ved omstart. Beslutningen gjelder fra "
    "neste sprint og er dokumentert i arkitekturnotatet."
)
SIGNATURE = "\n\nMvh\nHarald Daltveit\nTlf: 98251606\nharald@example.no"


def _pii_detector(has_pii: bool = True):
    async def fake_detect(text, threshold=0.40):
        return True, DetectionResult(
            content_type=ContentType.DECISION,
            confidence=0.9,
            suggested_scope="team",
            suggested_title="Session cache decision",
            has_pii=has_pii,
        )

    return fake_detect


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_PII_MODE", raising=False)
    yield


def test_pii_message_is_stored_with_identifiers_redacted(monkeypatch):
    import threadweave.api as api_mod

    monkeypatch.setattr(api_mod, "is_worth_saving_async", _pii_detector(True))
    resp = client.post("/api/v1/ingest", json={
        "content": f"{BODY}\n\nKontonummer 3569.15.05322 og tlf 98251606.",
        "source": "email",
        "tenant_id": "test-pii-redact",
    })
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["id"] != "rejected_pii"
    assert data["has_pii"] is True
    assert data["redacted"], "the response must report what was replaced"
    assert "bank_account" in data["redacted"]
    assert any("pii_redacted(" in s for s in data["signals"])


def test_reject_mode_still_rejects(monkeypatch):
    import threadweave.api as api_mod

    monkeypatch.setenv("THREADWEAVE_PII_MODE", "reject")
    monkeypatch.setattr(api_mod, "is_worth_saving_async", _pii_detector(True))
    resp = client.post("/api/v1/ingest", json={
        "content": f"{BODY}\n\nKontonummer 3569.15.05322.",
        "source": "email",
        "tenant_id": "test-pii-reject",
    })
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["id"] == "rejected_pii"
    assert data["should_save"] is False
    assert "pii_rejected" in data["signals"]


def test_identifier_only_content_is_still_rejected(monkeypatch):
    import threadweave.api as api_mod

    monkeypatch.setattr(api_mod, "is_worth_saving_async", _pii_detector(True))
    roster = "\n".join(f"{i:06d}{i:05d}" for i in range(1, 15))
    resp = client.post("/api/v1/ingest", json={
        "content": roster,
        "source": "email",
        "tenant_id": "test-pii-roster",
    })
    assert resp.status_code in (200, 201)
    assert resp.json()["id"] == "rejected_pii"


def test_signature_is_not_shown_to_the_detector(monkeypatch):
    """The detector must not see the signature block, which is what made a
    phone-number trigger fire on every signed message."""
    import threadweave.api as api_mod

    seen: list[str] = []

    async def fake_detect(text, threshold=0.40):
        seen.append(text)
        return True, DetectionResult(
            content_type=ContentType.DECISION,
            confidence=0.9,
            suggested_scope="team",
            suggested_title="Session cache decision",
        )

    monkeypatch.setattr(api_mod, "is_worth_saving_async", fake_detect)
    resp = client.post("/api/v1/ingest", json={
        "content": f"{BODY}{SIGNATURE}",
        "source": "email",
        "tenant_id": "test-signature-strip",
    })
    assert resp.status_code in (200, 201)
    assert seen, "detector was not called"
    assert "98251606" not in seen[0]
    assert "arkitekturnotatet" in seen[0]
