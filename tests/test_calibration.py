# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for affine probability calibration (Calibration, policy, gate wiring).

The numbers are measured: raw probabilities from every backend we have tested
miss most true positives at our gossip and PII bars, so calibration is what makes
a bar mean anything. These tests pin the mechanism, not the fitted values.
"""

from __future__ import annotations

import json

import pytest

from threadweave.decisions import (
    Calibration,
    ChoiceAnswer,
    DecisionEngine,
    DecisionPolicy,
    DecisionProvider,
    DecisionResponse,
    NoulAnswer,
    confidence_from_probabilities,
)


class StaticProvider(DecisionProvider):
    """Returns canned answers, so the gate can be tested without a backend."""

    name = "static"

    def __init__(self, answers):
        self._answers = answers

    def is_available(self) -> bool:
        return True

    def evaluate(self, state, questions):
        return DecisionResponse(provider=self.name, model="static", answers=dict(self._answers), latency_ms=1.0)


# ── the transform ────────────────────────────────────────────


def test_identity_leaves_probabilities_untouched():
    calibration = Calibration()
    assert calibration.is_identity
    for p in [0.0, 0.02, 0.5, 0.83, 1.0]:
        assert calibration.apply(p) == p


def test_offset_moves_the_crossover_point():
    # a temperature cannot do this: b shifts every logit.
    calibration = Calibration(a=1.0, b=2.0)
    assert calibration.apply(0.5) > 0.85
    assert not calibration.is_identity


def test_slope_expands_get_the_same_ordering():
    calibration = Calibration(a=3.0, b=0.0)
    values = [calibration.apply(p) for p in [0.1, 0.3, 0.5, 0.7, 0.9]]
    assert values == sorted(values)
    assert values[0] < 0.1 and values[-1] > 0.9


def test_extremes_are_clamped_not_nan():
    calibration = Calibration(a=4.0, b=6.0)
    assert calibration.apply(0.0) == pytest.approx(0.0, abs=0.02)
    assert calibration.apply(1.0) == pytest.approx(1.0, abs=0.02)
    assert 0.0 <= calibration.apply(-1.0) <= 1.0
    assert 0.0 <= calibration.apply(2.0) <= 1.0


def test_distribution_is_calibrated_then_renormalized():
    calibration = Calibration(a=2.0, b=1.0)
    probabilities = {"team": 0.5, "department": 0.3, "organization": 0.2}
    out = calibration.apply_distribution(probabilities)
    assert sum(out.values()) == pytest.approx(1.0)
    assert list(out) == list(probabilities)
    # the biggest option stays the biggest
    assert max(out, key=lambda key: out[key]) == "team"


def test_all_zero_distribution_is_returned_unchanged():
    calibration = Calibration(a=2.0, b=1.0)
    assert calibration.apply_distribution({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}


# ── env parsing ──────────────────────────────────────────────


def test_calibration_from_a_json_string(monkeypatch):
    monkeypatch.setenv(
        "THREADWEAVE_DECISION_CALIBRATION",
        json.dumps({"is_gossip": {"a": 2.95, "b": 6.5}, "has_pii": [0.8, 1.5]}),
    )
    policy = DecisionPolicy.from_env()
    assert policy.calibration["is_gossip"] == Calibration(a=2.95, b=6.5)
    assert policy.calibration["has_pii"] == Calibration(a=0.8, b=1.5)


def test_calibration_from_a_file(monkeypatch, tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"is_gossip": {"a": 1.5, "b": -0.5}}), encoding="utf-8")
    monkeypatch.setenv("THREADWEAVE_DECISION_CALIBRATION", str(path))
    policy = DecisionPolicy.from_env()
    assert policy.calibration["is_gossip"] == Calibration(a=1.5, b=-0.5)


def test_malformed_calibration_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_DECISION_CALIBRATION", "{not json")
    assert DecisionPolicy.from_env().calibration == {}
    monkeypatch.setenv("THREADWEAVE_DECISION_CALIBRATION", json.dumps(["nope"]))
    assert DecisionPolicy.from_env().calibration == {}
    monkeypatch.setenv("THREADWEAVE_DECISION_CALIBRATION", json.dumps({"is_gossip": {"a": "x"}}))
    assert DecisionPolicy.from_env().calibration == {}


def test_absent_calibration_means_no_calibration(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_DECISION_CALIBRATION", raising=False)
    assert DecisionPolicy.from_env().calibration == {}


# ── through the gate ─────────────────────────────────────────


def _gate(answers, policy):
    from threadweave.decision_gate import DecisionGate

    return DecisionGate(DecisionEngine(StaticProvider(answers), policy=policy), policy=policy, escalate=None)


@pytest.mark.asyncio
async def test_calibration_pushes_a_gossip_answer_over_the_reject_bar():
    policy = DecisionPolicy(calibration={"is_gossip": Calibration(a=3.0, b=4.0)})
    answers = {"is_gossip": NoulAnswer(noul=0.35), "has_pii": NoulAnswer(noul=0.1)}
    outcome = await _gate(answers, policy).evaluate(
        "You should hear what I heard about Lars, he is a nightmare to work with."
    )
    assert outcome.result.has_gossip is True
    assert any("calibrated(is_gossip)" in signal for signal in outcome.result.signals)


@pytest.mark.asyncio
async def test_without_calibration_the_answer_is_left_alone():
    policy = DecisionPolicy()
    answers = {"is_gossip": NoulAnswer(noul=0.35)}
    outcome = await _gate(answers, policy).evaluate(
        "You should hear what I heard about Lars, he is a nightmare to work with."
    )
    assert outcome.result.has_gossip is False
    assert not any("calibrated(" in signal for signal in outcome.result.signals)


@pytest.mark.asyncio
async def test_calibrating_a_choice_recomputes_choice_and_confidence():
    policy = DecisionPolicy(calibration={"content_type": Calibration(a=3.0, b=1.0)})
    probabilities = {"decision": 0.5, "chat": 0.4, "reference": 0.1}
    answers = {
        "content_type": ChoiceAnswer(
            choice="decision",
            probabilities=probabilities,
            confidence=confidence_from_probabilities(probabilities.values()),
        )
    }
    outcome = await _gate(answers, policy).evaluate(
        "After the review we decided to keep the nightly batch on Postgres."
    )
    assert outcome.result.content_type.value == "decision"
    assert outcome.result.confidence > confidence_from_probabilities(probabilities.values())
