# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the Laya decision provider (decision_laya.py).

No weights are downloaded: the router is injected as a fake that records the
request and returns canned answers in Laya's own wire shape.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Mapping

import pytest

from threadweave.decisions import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
)
from threadweave.decision_laya import (
    CHECKPOINT_ENGLISH,
    CHECKPOINT_MULTILINGUAL,
    CHECKPOINT_TYPED_DECISIONS,
    LayaDecisionProvider,
    LayaUnavailable,
)


class FakeRouter:
    """Records the request and returns whatever answers it was given."""

    def __init__(self, answers: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.answers = dict(answers or {})
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def predict(self, state: str, questions: Mapping[str, Any], model: str | None = None) -> Dict[str, Any]:
        self.calls.append({"state": state, "questions": dict(questions), "model": model})
        if self.error is not None:
            raise self.error
        return {"answers": self.answers, "usage": {"input_tokens": 42, "output_tokens": 0}, "routing": {"model": model}}


GOSSIP = Noul(
    instructions="Is this message about a person rather than about the work?",
    true_meaning="Targets or gossips about a person",
    false_meaning="Criticises work, systems or the situation itself",
    statement="The message contains gossip or an insult about a person.",
)


def provider(router, **kwargs):
    defaults = dict(router=router, language_detector=lambda text: type("G", (), {"language": "en"})())
    defaults.update(kwargs)
    return LayaDecisionProvider(**defaults)


# ── Choice ───────────────────────────────────────────────────


def test_choice_maps_probabilities_and_confidence():
    router = FakeRouter(
        {"scope": {"type": "choice", "choice": "organization",
                   "probabilities": {"team": 0.1, "department": 0.2, "organization": 0.7}}}
    )
    question = Choice(instructions="How widely does this apply?", options={"team": "One team", "department": "A department", "organization": "The organization"})
    response = provider(router).evaluate("state", {"scope": question})

    answer = response.answers["scope"]
    assert isinstance(answer, ChoiceAnswer)
    assert answer.choice == "organization"
    assert answer.probabilities == {"team": 0.1, "department": 0.2, "organization": 0.7}
    # (3 * 0.70 - 1) / 2 = 0.55, the same derivation the other providers use.
    assert answer.confidence == pytest.approx(0.55)
    assert response.provider == "laya"
    assert response.ok


def test_choice_wire_request_uses_option_descriptions():
    router = FakeRouter({"scope": {"choice": "team", "probabilities": {"team": 1.0, "organization": 0.0}}})
    question = Choice(instructions="How widely?", options={"team": "One team's workflow", "organization": "The whole organization"})
    provider(router).evaluate("state", {"scope": question})
    wire = router.calls[0]["questions"]["scope"]
    assert wire["type"] == "choice"
    assert wire["instructions"] == "How widely?"
    assert wire["criteria"] == {"team": "One team's workflow", "organization": "The whole organization"}


def test_choice_falls_back_to_the_chosen_key_when_no_distribution_comes_back():
    router = FakeRouter({"scope": {"choice": "team", "probabilities": {}}})
    question = Choice(instructions="How widely?", options={"team": "One team", "organization": "The organization"})
    answer = provider(router).evaluate("state", {"scope": question}).answers["scope"]
    assert answer.choice == "team"
    assert answer.probabilities["team"] == 1.0


# ── Noul ─────────────────────────────────────────────────────


def test_noul_is_asked_as_a_two_option_choice_by_default():
    router = FakeRouter({"g": {"probabilities": {"A": 0.83, "B": 0.17}, "choice": "A"}})
    answer = provider(router).evaluate("state", {"g": GOSSIP}).answers["g"]

    wire = router.calls[0]["questions"]["g"]
    assert wire["type"] == "choice"
    assert wire["criteria"] == {"A": "Targets or gossips about a person", "B": "Criticises work, systems or the situation itself"}
    assert isinstance(answer, NoulAnswer)
    assert answer.noul == pytest.approx(0.83)
    assert answer.confidence == pytest.approx(0.66)
    assert answer.is_true


def test_noul_native_form_is_available_when_asked_for():
    router = FakeRouter({"g": {"type": "noul", "noul": 0.31, "confidence": 0.69}})
    answer = provider(router, noul_form="noul").evaluate("state", {"g": GOSSIP}).answers["g"]
    assert router.calls[0]["questions"]["g"] == {"type": "noul", "instructions": GOSSIP.instructions}
    assert answer.noul == pytest.approx(0.31)
    # Our confidence semantics, not Laya's: |2 * 0.31 - 1| = 0.38, not 0.69.
    assert answer.confidence == pytest.approx(0.38)


def test_noul_without_meanings_uses_the_native_form():
    plain = Noul(instructions="Does this contain gossip?", statement="The message contains gossip.")
    router = FakeRouter({"g": {"noul": 0.62}})
    answer = provider(router).evaluate("state", {"g": plain}).answers["g"]
    assert router.calls[0]["questions"]["g"]["type"] == "noul"
    assert answer.noul == pytest.approx(0.62)


# ── Score ────────────────────────────────────────────────────


def test_score_maps_to_our_rubric_answer():
    router = FakeRouter(
        {"u": {"type": "score", "score": 1.7, "legend": {"0": "not urgent", "1": "soon", "2": "blocking"},
               "probabilities": {"0": 0.03, "1": 0.24, "2": 0.73}}}
    )
    question = Score(instructions="How urgent?", levels=["not urgent", "soon", "blocking"])
    answer = provider(router).evaluate("state", {"u": question}).answers["u"]

    assert router.calls[0]["questions"]["u"]["criteria"] == ["not urgent", "soon", "blocking"]
    assert isinstance(answer, ScoreAnswer)
    assert answer.score == pytest.approx(1.7)
    assert answer.legend == {"0": "not urgent", "1": "soon", "2": "blocking"}
    assert answer.probabilities["2"] == pytest.approx(0.73)


# ── checkpoint routing ───────────────────────────────────────


def test_english_text_uses_the_english_checkpoint():
    router = FakeRouter({"g": {"probabilities": {"A": 0.5, "B": 0.5}}})
    provider(router).evaluate("state", {"g": GOSSIP})
    assert router.calls[0]["model"] == CHECKPOINT_ENGLISH


def test_norwegian_text_uses_the_multilingual_checkpoint():
    router = FakeRouter({"g": {"probabilities": {"A": 0.5, "B": 0.5}}})
    norwegian = type("G", (), {"language": "no"})()
    provider(router, language_detector=lambda text: norwegian).evaluate("state", {"g": GOSSIP})
    assert router.calls[0]["model"] == CHECKPOINT_MULTILINGUAL


def test_unknown_language_uses_the_multilingual_checkpoint():
    router = FakeRouter({"g": {"probabilities": {"A": 0.5, "B": 0.5}}})
    unknown = type("G", (), {"language": ""})()
    provider(router, language_detector=lambda text: unknown).evaluate("state", {"g": GOSSIP})
    assert router.calls[0]["model"] == CHECKPOINT_MULTILINGUAL


def test_an_explicit_model_pins_one_checkpoint():
    router = FakeRouter({"g": {"probabilities": {"A": 0.5, "B": 0.5}}})
    norwegian = type("G", (), {"language": "no"})()
    provider(router, model=CHECKPOINT_TYPED_DECISIONS, language_detector=lambda text: norwegian).evaluate("state", {"g": GOSSIP})
    assert router.calls[0]["model"] == CHECKPOINT_TYPED_DECISIONS


def test_real_language_detector_selects_checkpoints_without_a_stub():
    router = FakeRouter({"g": {"probabilities": {"A": 0.5, "B": 0.5}}})
    p = LayaDecisionProvider(router=router)
    p.evaluate("The migration script fails on every third batch because the retry logic hides the real error.", {"g": GOSSIP})
    p.evaluate("Migrasjonsskriptet feiler på hver tredje batch fordi feilhåndteringen skjuler årsaken.", {"g": GOSSIP})
    assert [call["model"] for call in router.calls] == [CHECKPOINT_ENGLISH, CHECKPOINT_MULTILINGUAL]


# ── failure handling ─────────────────────────────────────────


def test_one_missing_answer_does_not_lose_the_others():
    router = FakeRouter({"g": {"probabilities": {"A": 0.9, "B": 0.1}}})
    questions = {"g": GOSSIP, "scope": Choice(instructions="How widely?", options={"team": "One team", "organization": "All"})}
    response = provider(router).evaluate("state", questions)
    assert set(response.answers) == {"g"}
    assert response.ok


def test_a_router_error_returns_an_error_response_instead_of_raising():
    router = FakeRouter(error=RuntimeError("cuda out of memory"))
    response = provider(router).evaluate("state", {"g": GOSSIP})
    assert not response.ok
    assert "cuda out of memory" in response.error
    assert response.answers == {}


def test_a_missing_package_raises_laya_unavailable():
    def factory():
        raise ImportError("No module named 'laya'")

    p = LayaDecisionProvider(router_factory=factory)
    with pytest.raises(LayaUnavailable):
        p.evaluate("state", {"g": GOSSIP})


def test_is_available_is_false_when_the_package_is_missing(monkeypatch):
    # None in sys.modules makes ``import laya`` raise ImportError.
    monkeypatch.setitem(sys.modules, "laya", None)
    assert LayaDecisionProvider().is_available() is False


def test_is_available_is_true_with_an_injected_router():
    assert LayaDecisionProvider(router=FakeRouter()).is_available() is True


# ── bookkeeping ──────────────────────────────────────────────


def test_latency_usage_and_call_count_are_recorded():
    router = FakeRouter({"g": {"probabilities": {"A": 0.9, "B": 0.1}}})
    p = provider(router)
    response = p.evaluate("state", {"g": GOSSIP})
    assert p.calls == 1
    assert response.usage["input_tokens"] == 42
    assert response.latency_ms >= 0
    assert p.last_checkpoints == [CHECKPOINT_ENGLISH]


def test_provider_declares_it_cannot_answer_the_language_question():
    # Their router misroutes Latin-script languages (measured), so the gate must
    # answer the language question with language_id.py instead of asking here.
    assert LayaDecisionProvider.answers_language is False


# ── factory ──────────────────────────────────────────────────


def test_factory_builds_the_laya_provider(monkeypatch):
    from threadweave import decision_providers

    monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "laya")
    monkeypatch.setenv("THREADWEAVE_DECISION_LAYA_MODEL", CHECKPOINT_TYPED_DECISIONS)
    monkeypatch.setenv("THREADWEAVE_DECISION_LAYA_NOUL_FORM", "noul")
    monkeypatch.setattr(decision_providers, "_provider", None)

    p = decision_providers.get_decision_provider()
    try:
        assert isinstance(p, LayaDecisionProvider)
        assert p.model == CHECKPOINT_TYPED_DECISIONS
        assert p.noul_form == "noul"
    finally:
        decision_providers._provider = None

# ── through the gate ─────────────────────────────────────────


class TestGateWithLaya:
    @pytest.mark.asyncio
    async def test_gate_uses_laya_answers_and_our_language_detector(self):
        from threadweave.decision_gate import DecisionGate
        from threadweave.decisions import DecisionEngine, DecisionPolicy

        router = FakeRouter(
            {
                "content_type": {
                    "type": "choice",
                    "choice": "decision",
                    "probabilities": {
                        "decision": 0.72,
                        "reference": 0.12,
                        "answer": 0.08,
                        "question": 0.05,
                        "chat": 0.03,
                    },
                },
                "is_gossip": {"probabilities": {"A": 0.91, "B": 0.09}},
                "has_pii": {"probabilities": {"A": 0.03, "B": 0.97}},
                "scope": {"type": "choice", "choice": "team", "probabilities": {"team": 0.9, "department": 0.06, "organization": 0.04}},
            }
        )
        provider = LayaDecisionProvider(router=router)
        gate = DecisionGate(
            DecisionEngine(provider, policy=DecisionPolicy()),
            policy=DecisionPolicy(),
            escalate=None,
        )
        outcome = await gate.evaluate(
            "Etter tre uker med lasttesting besluttet vi å flytte sesjonscachen til Redis."
        )

        assert outcome.result.content_type.value == "decision"
        # language is never asked of Laya (their router misroutes these), so the
        # gate answers it with the deterministic detector.
        assert outcome.result.language == "no"
        assert outcome.result.has_gossip is True
        assert outcome.result.has_pii is False
        assert router.calls[0]["model"] == CHECKPOINT_MULTILINGUAL
        assert not any(qid == "language" for qid in router.calls[0]["questions"])
