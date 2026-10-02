# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for the encoder decision provider (decision_encoder.py).

No model weights are downloaded here: the provider takes an injected pipeline,
which is the seam under test. The real pipeline is exercised by the live probe
script, not by the suite.
"""

import pytest

from threadweave import decision_providers as prov_mod
from threadweave.decision_encoder import (
    DEFAULT_CHOICE_TEMPLATE,
    DEFAULT_ENCODER_MODEL,
    EncoderDecisionProvider,
    EncoderUnavailable,
)
from threadweave.decision_gate import DecisionGate, build_ingest_questions
from threadweave.decisions import (
    Choice,
    DecisionEngine,
    DecisionPolicy,
    Noul,
    NoulAnswer,
    Score,
)
from threadweave.detector import ContentType


# ── Fakes ────────────────────────────────────────────────────


class FakePipeline:
    """Stand-in for the HF zero-shot pipeline; records every call."""

    def __init__(self, scores=None, default=0.5, reverse_order=False, boom_on=None):
        self.scores = scores or {}
        self.default = default
        self.reverse_order = reverse_order
        self.boom_on = boom_on
        self.calls = []

    def __call__(self, state, candidate_labels=None, hypothesis_template=None, multi_label=None):
        labels = list(candidate_labels or [])
        self.calls.append(
            {
                "state": state,
                "labels": labels,
                "template": hypothesis_template,
                "multi_label": multi_label,
            }
        )
        if self.boom_on and self.boom_on in labels:
            raise RuntimeError("model exploded")
        if self.reverse_order:
            labels = labels[::-1]
        return {
            "labels": labels,
            "scores": [self.scores.get(label, self.default) for label in labels],
        }


@pytest.fixture(autouse=True)
def clean_provider_cache():
    prov_mod.reset_decision_provider()
    yield
    prov_mod.reset_decision_provider()


def provider_with(**kwargs):
    pipeline = kwargs.pop("pipeline", None) or FakePipeline(**kwargs)
    provider = EncoderDecisionProvider(model="fake/encoder", pipeline=pipeline)
    return provider, pipeline


# ── Choice ───────────────────────────────────────────────────


class TestChoice:
    def test_uses_descriptions_as_labels_without_multi_label(self):
        provider, pipeline = provider_with(
            scores={"A decision with reasoning": 0.9, "Casual chat": 0.1}
        )
        question = Choice(
            instructions="What is this?",
            options={"decision": "A decision with reasoning", "chat": "Casual chat"},
        )
        answer = provider.evaluate("text", {"content_type": question}).answers["content_type"]

        call = pipeline.calls[0]
        assert call["labels"] == ["A decision with reasoning", "Casual chat"]
        assert call["template"] == DEFAULT_CHOICE_TEMPLATE
        assert call["multi_label"] is False
        assert answer.choice == "decision"
        assert answer.probabilities == pytest.approx({"decision": 0.9, "chat": 0.1})
        assert answer.confidence == pytest.approx(0.8)

    def test_option_without_description_falls_back_to_the_key(self):
        provider, pipeline = provider_with(scores={"decision": 0.95, "chat": 0.05})
        question = Choice(
            instructions="What is this?", options={"decision": None, "chat": None}
        )
        answer = provider.evaluate("text", {"content_type": question}).answers["content_type"]
        assert pipeline.calls[0]["labels"] == ["decision", "chat"]
        assert answer.choice == "decision"

    def test_duplicate_descriptions_still_map_back_to_distinct_options(self):
        provider, pipeline = provider_with(scores={"a: same text": 0.7, "b: same text": 0.3})
        question = Choice(
            instructions="?",
            options={"a": "same text", "b": "same text"},
        )
        answer = provider.evaluate("text", {"content_type": question}).answers["content_type"]
        assert pipeline.calls[0]["labels"] == ["same text", "b: same text"]
        assert answer.choice in {"a", "b"}
        assert set(answer.probabilities) == {"a", "b"}

    def test_scores_are_normalized_even_if_the_model_returns_unfocused_ones(self):
        provider, _ = provider_with(scores={"x": 0.5, "y": 0.5, "z": 0.0})
        question = Choice(instructions="?", options={"x": None, "y": None, "z": None})
        answer = provider.evaluate("text", {"content_type": question}).answers["content_type"]
        assert sum(answer.probabilities.values()) == pytest.approx(1.0)


# ── Noul ─────────────────────────────────────────────────────


class TestNoul:
    def test_uses_the_declarative_statement_and_multi_label(self):
        statement = "This message gossips about a person."
        provider, pipeline = provider_with(scores={statement: 0.93})
        question = Noul(instructions="Is this gossip?", statement=statement)
        answer = provider.evaluate("text", {"is_gossip": question}).answers["is_gossip"]

        call = pipeline.calls[0]
        assert call["labels"] == [statement]
        assert call["multi_label"] is True
        assert call["template"] == "{}"
        assert isinstance(answer, NoulAnswer)
        assert answer.noul == pytest.approx(0.93)

    def test_falls_back_to_the_question_when_no_statement_is_given(self):
        provider, pipeline = provider_with(scores={"Is this gossip?": 0.2})
        answer = provider.evaluate(
            "text", {"is_gossip": Noul(instructions="Is this gossip?")}
        ).answers["is_gossip"]
        assert pipeline.calls[0]["labels"] == ["Is this gossip?"]
        assert answer.noul == pytest.approx(0.2)

    def test_probability_is_clamped_and_a_missing_label_yields_no_answer(self):
        provider, _ = provider_with(scores={"s": 1.4})
        question = Noul(instructions="?", statement="s")
        assert provider.evaluate("text", {"q": question}).answers["q"].noul == 1.0

        class EmptyPipeline:
            """A pipeline that answers about a label it was not asked about."""

            def __call__(self, state, candidate_labels=None, hypothesis_template=None, multi_label=None):
                return {"labels": ["something else"], "scores": [0.9]}

        provider2 = EncoderDecisionProvider(model="fake/encoder", pipeline=EmptyPipeline())
        missing = provider2.evaluate("text", {"q": Noul(instructions="?", statement="absent")})
        assert "q" not in missing.answers

    def test_statement_is_not_sent_to_the_hosted_provider_schema(self):
        question = Noul(instructions="Is this gossip?", statement="This is gossip.")
        assert "statement" not in question.to_wire()


# ── Score ────────────────────────────────────────────────────


class TestScore:
    def test_levels_are_judged_independently_then_normalized(self):
        levels = ["calm", "annoyed", "furious"]
        provider, pipeline = provider_with(scores={"calm": 0.8, "annoyed": 0.3, "furious": 0.1})
        answer = provider.evaluate(
            "text", {"frustration": Score(instructions="How annoyed?", levels=levels)}
        ).answers["frustration"]

        call = pipeline.calls[0]
        assert call["labels"] == levels
        assert call["multi_label"] is True  # each level on its own
        assert answer.probabilities == pytest.approx(
            {"0": 0.8 / 1.2, "1": 0.3 / 1.2, "2": 0.1 / 1.2}
        )
        assert answer.score == pytest.approx(0.4166, abs=1e-3)
        assert answer.legend == {"0": "calm", "1": "annoyed", "2": "furious"}

    def test_equal_level_scores_give_the_middle_with_zero_confidence(self):
        provider, _ = provider_with(scores={"a": 0.5, "b": 0.5, "c": 0.5})
        answer = provider.evaluate(
            "text", {"s": Score(instructions="?", levels=["a", "b", "c"])}
        ).answers["s"]
        assert answer.score == pytest.approx(1.0)
        assert answer.confidence == pytest.approx(0.0)


# ── Provider plumbing ────────────────────────────────────────


class TestProviderPlumbing:
    def test_one_call_per_question_no_packing(self):
        provider, pipeline = provider_with(default=0.2)
        questions = build_ingest_questions()
        provider.evaluate("some message", questions)
        assert len(pipeline.calls) == len(questions)

    def test_a_failing_question_does_not_lose_the_others(self):
        pipeline = FakePipeline(scores={"fine": 0.9}, boom_on="explode")
        provider = EncoderDecisionProvider(model="fake/encoder", pipeline=pipeline)
        response = provider.evaluate(
            "text",
            {"bad": Noul(instructions="?", statement="explode"), "good": Noul(instructions="?", statement="fine")},
        )
        assert "good" in response.answers and "bad" not in response.answers
        assert response.provider == "encoder" and response.ok

    def test_missing_runtime_raises_with_an_install_hint(self):
        def factory():
            raise ImportError("No module named 'transformers'")

        provider = EncoderDecisionProvider(model="x", pipeline_factory=factory)
        with pytest.raises(EncoderUnavailable) as exc:
            provider.evaluate("text", {"q": Noul(instructions="?", statement="s")})
        assert "transformers" in str(exc.value) and "uv pip install" in str(exc.value)

    def test_is_available_is_false_without_a_model(self):
        assert EncoderDecisionProvider(model="", pipeline=FakePipeline()).is_available() is False
        assert EncoderDecisionProvider(model="fake/encoder", pipeline=FakePipeline()).is_available()

    def test_is_available_does_not_load_weights(self):
        # Availability is gated on the optional decision runtime, so this
        # only means anything where the runtime is installed (the `decisions`
        # extra). Skipping keeps a clean dev checkout green for the right
        # reason instead of failing on a missing optional dependency.
        pytest.importorskip("torch")
        pytest.importorskip("transformers")
        built = []

        provider = EncoderDecisionProvider(
            model="fake/encoder", pipeline_factory=lambda: built.append(1) or FakePipeline()
        )
        assert provider.is_available() is True
        assert built == []  # no load on a health check

    def test_latency_is_reported(self):
        provider, _ = provider_with(default=0.3)
        response = provider.evaluate("text", {"q": Noul(instructions="?", statement="s")})
        assert response.latency_ms > 0


class TestFactory:
    def test_encoder_selected_from_env(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "encoder")
        provider = prov_mod.get_decision_provider()
        assert isinstance(provider, EncoderDecisionProvider)
        assert provider.model == DEFAULT_ENCODER_MODEL

    def test_model_override_from_env(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "encoder")
        monkeypatch.setenv("THREADWEAVE_DECISION_MODEL", "MoritzLaurer/ModernBERT-base-zeroshot-v2.0")
        provider = prov_mod.get_decision_provider()
        assert provider.model == "MoritzLaurer/ModernBERT-base-zeroshot-v2.0"


# ── Gate integration ─────────────────────────────────────────


class TestGateWithEncoder:
    @pytest.mark.asyncio
    async def test_gate_policy_applies_to_encoder_answers(self):
        pipeline = FakePipeline(
            scores={
                # content_type options are described, so labels are descriptions
                "A choice that was made, an approval or rejection, with the reasoning "
                "behind it": 0.88,
                "Small talk, greetings, acknowledgements, scheduling noise": 0.12,
                "The message contains gossip or an insult about a person.": 0.07,
                "The message contains personal data such as a national identity "
                "number or a private phone number.": 0.04,
                "Norwegian (bokmål or nynorsk)": 0.9,
                "English": 0.05,
                "A company-wide policy, standard or practice": 0.9,
                "Specific to one team's workflow, tools or conventions": 0.05,
            },
            default=0.01,
        )
        provider = EncoderDecisionProvider(model="fake/encoder", pipeline=pipeline)
        gate = DecisionGate(
            DecisionEngine(provider, policy=DecisionPolicy()),
            policy=DecisionPolicy(),
            escalate=None,
        )
        outcome = await gate.evaluate(
            "Etter tre uker med lasttesting besluttet vi å flytte sesjonscachen til Redis."
        )

        assert outcome.result.content_type is ContentType.DECISION
        assert outcome.result.language == "no"
        assert outcome.result.suggested_scope == "organization"
        assert outcome.result.has_gossip is False
        assert outcome.result.has_pii is False
        # the declarative statement, not the question, was the hypothesis
        gossip_call = next(
            call for call in pipeline.calls if len(call["labels"]) == 1 and "insult about a person" in call["labels"][0]
        )
        assert gossip_call["template"] == "{}"
