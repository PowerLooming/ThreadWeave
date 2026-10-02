# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Pattern-shaped PII must not need a model's permission to be redacted.

The gate answers ``has_pii`` as a model opinion against a bar (0.75 by default)
and the ingest redaction is gated on that opinion. Measured on real mail the
local encoder scores a message carrying a Norwegian mobile around 0.06, so the
verdict came back clean and the message was stored with the number intact and
``redacted: null`` (observed live 2026-10-02, version 0.4.17, provider encoder).

These tests pin the fusion that closes that path. The integration test drives the
real ingest endpoint with no provider configured and no verdict monkeypatched, so
it fails if the identifier patterns ever lose their say again.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadweave.api import app
from threadweave.detector import ContentType, DetectionResult, detect, fuse_pattern_pii

client = TestClient(app)

BODY = (
    "Session cache decision: we decided to move the session cache to Redis "
    "because the load was too high at the end of the day and we lose sessions on "
    "restart. The decision applies from next sprint and is documented in the "
    "architecture note."
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_PII_MODE", raising=False)
    monkeypatch.delenv("THREADWEAVE_PII_REDACT_KINDS", raising=False)
    yield


def test_regex_engine_fuses_identifier_evidence():
    result = detect(f"{BODY}\n\nThe site contact is on 90000002, customer no 123456.")
    assert result.has_pii is True
    assert any(s.startswith("pii_patterns(") for s in result.signals)


def test_clean_text_is_left_alone():
    result = detect(BODY)
    assert result.has_pii is False
    assert not any(s.startswith("pii_patterns(") for s in result.signals)


def test_transaction_reference_is_not_identifier_evidence():
    """A reference number is not a person's identifier."""
    result = detect(f"{BODY}\n\nFakturanr 99887766 is already sent.")
    assert result.has_pii is False


def test_build_number_is_not_identifier_evidence():
    """A bank-account shape after a build label is a build number.

    Masking it would corrupt the stored knowledge for no protection, which is the
    trade the earlier detector contract already made: "when in doubt, don't match"
    (that rule was written when the verdict deleted the message; it survives the
    move to redaction because a reference is not a person).
    """
    result = detect(
        "The build number 1234.56.78901 was deployed to production yesterday "
        "after passing all integration tests."
    )
    assert result.has_pii is False


def test_support_ticket_number_is_not_identifier_evidence():
    """The two entries the live sweep flagged were both this shape."""
    for line in (
        "Your service request number is 4567890123456789 in case you need it.",
        "Support request number: 4567890123456789",
    ):
        assert detect(f"{BODY}\n\n{line}").has_pii is False


def test_fusion_is_idempotent():
    """The async wrappers fuse a result an engine may already have fused."""
    once = fuse_pattern_pii(
        DetectionResult(content_type=ContentType.ANSWER, confidence=0.9),
        f"{BODY}\n\nCall 90000002.",
    )
    twice = fuse_pattern_pii(once, f"{BODY}\n\nCall 90000002.")
    assert sum(s.startswith("pii_patterns(") for s in twice.signals) == 1
    assert once.has_pii is True


async def test_async_wrapper_fuses_a_clean_model_verdict(monkeypatch):
    """The live failure mode: the provider says clean, the patterns say otherwise."""
    import threadweave.decision_gate as gate_mod
    from threadweave.detector import is_worth_saving_async

    class _CleanGate:
        async def is_worth_saving(self, text, threshold=0.40):
            return True, DetectionResult(
                content_type=ContentType.ANSWER, confidence=0.9, has_pii=False
            )

    monkeypatch.setattr(gate_mod, "get_decision_gate", lambda: _CleanGate())
    should_save, result = await is_worth_saving_async(f"{BODY}\n\nCall 90000002.")
    assert should_save is True
    assert result.has_pii is True


def test_ingest_redacts_pattern_pii_without_a_model_verdict():
    resp = client.post(
        "/api/v1/ingest",
        json={
            "content": f"{BODY}\n\nCall 90000002 or use Kundenr 123456.",
            "source": "email",
            "tenant_id": "test-pii-fusion",
        },
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["should_save"] is True, "the fixture must be a knowledge entry"
    assert data["has_pii"] is True
    assert data["redacted"] == {"labelled_identifier": 1, "mobile_bare": 1}

    stored = client.get(f"/api/v1/entries/{data['id']}").json()["content"]
    assert "[mobile_bare]" in stored
    assert "[labelled_identifier]" in stored
    assert "90000002" not in stored
    assert "123456" not in stored


def test_narrowed_kinds_do_not_fire_the_verdict(monkeypatch):
    """The verdict and the redactor act on the same kind set.

    In reject mode a verdict broader than the configured kinds would delete mail
    for a kind the deployment had deselected, so a narrowed deployment must not
    flag what it will not mask.
    """
    monkeypatch.setenv("THREADWEAVE_PII_REDACT_KINDS", "mobile_bare")
    resp = client.post(
        "/api/v1/ingest",
        json={
            "content": f"{BODY}\n\nThe reference Kundenr 123456 stays as it is.",
            "source": "email",
            "tenant_id": "test-pii-narrowed",
        },
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["has_pii"] is False
    assert data["redacted"] is None
