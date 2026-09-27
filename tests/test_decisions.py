# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for the typed decision layer (decisions.py, decision_providers.py,
decision_gate.py).

Contract under test: the gate answers ingest questions as typed decisions,
policy thresholds live in code, a rejection needs a high bar, and the whole
layer is inert unless THREADWEAVE_DECISION_PROVIDER is configured.
"""

import json
from pathlib import Path

import pytest

from threadweave import decision_gate as gate_mod
from threadweave import decision_providers as prov_mod
from threadweave.decision_gate import (
    DecisionGate,
    build_ingest_questions,
    effective_min_length,
)
from threadweave.decision_providers import (
    OllamaDecisionProvider,
    RemoteContentNotAllowed,
    TypeSafeDecisionProvider,
)
from threadweave.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionAudit,
    DecisionEngine,
    DecisionPolicy,
    DecisionProvider,
    DecisionResponse,
    Noul,
    NoulAnswer,
    QuestionError,
    Score,
    confidence_from_probabilities,
    normalize_probabilities,
)
from threadweave.detector import ContentType, DetectionResult, detect as regex_detect


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_decision_state(tmp_path, monkeypatch):
    """Keep the audit file and provider singletons out of real state."""
    monkeypatch.setenv("THREADWEAVE_DECISION_AUDIT", str(tmp_path / "decisions.jsonl"))
    monkeypatch.delenv("THREADWEAVE_DECISION_PROVIDER", raising=False)
    monkeypatch.delenv("THREADWEAVE_DECISION_ALLOW_REMOTE", raising=False)
    gate_mod.reset_decision_gate()
    prov_mod.reset_decision_provider()
    yield tmp_path / "decisions.jsonl"
    gate_mod.reset_decision_gate()
    prov_mod.reset_decision_provider()


class FakeProvider(DecisionProvider):
    """Provider returning canned answers; counts calls to prove batching."""

    name = "fake"
    model = "fake-1"

    def __init__(self, answers=None, error=None):
        self._answers = answers or {}
        self._error = error
        self.calls = []
        self.closed = False

    def is_available(self):
        return True

    def evaluate(self, state, questions):
        self.calls.append({"state": state, "questions": dict(questions)})
        if self._error:
            raise RuntimeError(self._error)
        return DecisionResponse(
            provider=self.name, model=self.model, answers=dict(self._answers)
        )

    def close(self):
        self.closed = True


def choice_answer(choice, confidence=0.9, probabilities=None):
    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities or {choice: confidence, "other": 1 - confidence},
        confidence=confidence,
    )


def gate_for(answers, escalate=None, policy=None):
    engine = DecisionEngine(FakeProvider(answers), policy=policy or DecisionPolicy())
    return DecisionGate(engine, escalate=escalate, policy=policy or DecisionPolicy()), engine


# ── Primitives ───────────────────────────────────────────────


class TestConfidence:
    def test_concentrated_distribution_is_certain(self):
        assert confidence_from_probabilities([1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_even_split_is_zero_confidence(self):
        assert confidence_from_probabilities([1 / 3, 1 / 3, 1 / 3]) == pytest.approx(0.0)

    def test_two_options_halfway(self):
        assert confidence_from_probabilities([0.7, 0.3]) == pytest.approx(0.4)

    def test_single_option_is_certain_not_a_divide_by_zero(self):
        assert confidence_from_probabilities([0.42]) == 1.0

    def test_empty_distribution_has_no_confidence(self):
        assert confidence_from_probabilities([]) == 0.0

    def test_known_example_matches_documented_derivation(self):
        # TypeSafe's documented derivation for three options at 90/6/4:
        # (3 * 0.90 - 1) / 2 = 0.85.
        assert confidence_from_probabilities([90, 6, 4]) == pytest.approx(0.85, abs=1e-6)


class TestNormalizeProbabilities:
    def test_renormalizes_and_drops_garbage(self):
        out = normalize_probabilities({"a": 2, "b": "x", "c": -1}, ["a", "b", "c"])
        assert out["a"] == pytest.approx(1.0)
        assert out["b"] == 0.0 and out["c"] == 0.0

    def test_all_zero_becomes_uniform_not_certainty(self):
        out = normalize_probabilities({}, ["a", "b", "c"])
        assert all(abs(v - 1 / 3) < 1e-9 for v in out.values())

    def test_missing_keys_are_filled(self):
        out = normalize_probabilities({"a": 0.5, "b": 0.5}, ["a", "b", "c"])
        assert set(out) == {"a", "b", "c"} and out["c"] == 0.0


class TestQuestionWireFormat:
    def test_noul_matches_typesafe_shape(self):
        q = Noul(instructions="Is this urgent?", true_meaning="time critical")
        wire = q.to_wire()
        assert wire["type"] == "noul"
        assert wire["instructions"] == "Is this urgent?"
        assert wire["criteria"] == {"true": "time critical"}

    def test_choice_carries_options_as_criteria(self):
        q = Choice(instructions="Which team?", options={"billing": "payments", "tech": None})
        wire = q.to_wire()
        assert wire["type"] == "choice"
        assert wire["criteria"] == {"billing": "payments", "tech": None}

    def test_score_carries_ordered_levels(self):
        q = Score(instructions="How bad?", levels=["calm", "frustrated", "angry"])
        assert q.to_wire()["criteria"] == ["calm", "frustrated", "angry"]

    def test_choice_needs_options(self):
        with pytest.raises(QuestionError):
            Choice(instructions="x", options={})

    def test_score_needs_two_levels_and_at_most_ten(self):
        with pytest.raises(QuestionError):
            Score(instructions="x", levels=["only one"])
        with pytest.raises(QuestionError):
            Score(instructions="x", levels=[str(i) for i in range(11)])

    def test_ingest_question_set_is_batched_and_named(self):
        questions = build_ingest_questions()
        assert set(questions) == {"content_type", "is_gossip", "has_pii", "language", "scope"}
        assert isinstance(questions["content_type"], Choice)
        assert isinstance(questions["is_gossip"], Noul)


class TestNoulAnswer:
    def test_confidence_is_distance_from_a_coin_flip(self):
        assert NoulAnswer(noul=1.0).confidence == 1.0
        assert NoulAnswer(noul=0.5).confidence == 0.0
        assert NoulAnswer(noul=0.9).confidence == pytest.approx(0.8)


# ── Engine + audit ───────────────────────────────────────────


class TestEngine:
    def test_ask_sends_every_question_in_one_call(self):
        provider = FakeProvider({"is_gossip": NoulAnswer(noul=0.1)})
        engine = DecisionEngine(provider)
        questions = build_ingest_questions()
        engine.ask("some message", questions)

        assert len(provider.calls) == 1
        assert set(provider.calls[0]["questions"]) == set(questions)

    def test_provider_fault_degrades_without_raising(self):
        engine = DecisionEngine(FakeProvider(error="connection refused"))
        response = engine.ask("text", {"is_gossip": Noul(instructions="?")})
        assert response.ok is False
        assert "connection refused" in response.error

    def test_empty_question_set_is_a_programming_error(self):
        engine = DecisionEngine(FakeProvider())
        with pytest.raises(QuestionError):
            engine.ask("text", {})

    def test_audit_line_records_hash_and_confidence(self, isolated_decision_state):
        engine = DecisionEngine(FakeProvider({"is_gossip": NoulAnswer(noul=0.93)}))
        engine.ask("Lars is a nightmare to work with", {"is_gossip": Noul(instructions="?")})

        lines = isolated_decision_state.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["purpose"] == "decision"
        assert entry["provider"] == "fake"
        assert entry["answers"]["is_gossip"]["noul"] == pytest.approx(0.93, abs=1e-3)
        assert len(entry["content_sha256"]) == 64
        assert entry["excerpt"].startswith("Lars is a nightmare")

    def test_audit_can_store_hash_without_content(self, tmp_path):
        audit = DecisionAudit(path=tmp_path / "a.jsonl", store_excerpt=False)
        engine = DecisionEngine(FakeProvider(), audit=audit)
        engine.ask("secret body", {"is_gossip": Noul(instructions="?")})

        entry = json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8").strip())
        assert "excerpt" not in entry

    def test_audit_write_failure_is_survivable(self, tmp_path):
        # A directory where the log file should be: the write must fail softly.
        blocked = tmp_path / "blocked.jsonl"
        blocked.mkdir()
        engine = DecisionEngine(
            FakeProvider({"is_gossip": NoulAnswer(noul=0.1)}),
            audit=DecisionAudit(path=blocked),
        )
        response = engine.ask("text", {"is_gossip": Noul(instructions="?")})
        assert response.ok  # the decision still happened
        assert response.answers["is_gossip"].noul == pytest.approx(0.1)


class TestPolicy:
    def test_defaults_match_documented_rejection_bars(self):
        policy = DecisionPolicy()
        assert policy.gossip_reject_at == 0.80
        assert policy.pii_reject_at == 0.75
        assert policy.gossip_review_at < policy.gossip_reject_at

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_MIN_CONFIDENCE", "0.7")
        monkeypatch.setenv("THREADWEAVE_DECISION_GOSSIP_REJECT_AT", "0.9")
        policy = DecisionPolicy.from_env()
        assert policy.min_confidence == 0.7
        assert policy.gossip_reject_at == 0.9

    def test_garbage_env_value_keeps_default(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_MIN_CONFIDENCE", "soon")
        assert DecisionPolicy.from_env().min_confidence == 0.55


# ── Ollama provider ──────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, **kwargs):  # noqa: A002 - httpx signature
        self.calls.append({"url": url, "json": json})
        return self._responses.pop(0)

    def close(self):
        pass


def ollama_payload(message_content, model="qwen3.5:9b"):
    return {
        "model": model,
        "message": {"content": message_content},
        "prompt_eval_count": 120,
        "eval_count": 40,
    }


class TestOllamaProvider:
    def _provider(self, client, **kw):
        return OllamaDecisionProvider(
            base_url="http://localhost:11434/v1", model="qwen3.5:9b", client=client, **kw
        )

    def test_one_batched_request_with_schema_pinned(self):
        content = json.dumps(
            {
                "content_type": {
                    "choice": "decision",
                    "probabilities": {"decision": 0.86, "answer": 0.14},
                },
                "is_gossip": {"noul": 0.03},
            }
        )
        client = FakeClient([FakeResponse(ollama_payload(content))])
        provider = self._provider(client)
        questions = {
            "content_type": Choice(instructions="?", options={"decision": "", "answer": ""}),
            "is_gossip": Noul(instructions="?"),
        }

        response = provider.evaluate("We decided to move to Postgres", questions)

        assert len(client.calls) == 1
        assert client.calls[0]["url"] == "http://localhost:11434/api/chat"
        payload = client.calls[0]["json"]
        assert isinstance(payload["format"], dict)  # schema, not just "json"
        assert payload["think"] is False
        assert set(payload["format"]["required"]) == {"content_type", "is_gossip"}
        assert response.provider == "ollama"
        assert response.usage == {"input_tokens": 120, "output_tokens": 40}

    def test_answers_are_typed_with_derived_confidence(self):
        content = json.dumps(
            {
                "content_type": {
                    "choice": "decision",
                    "probabilities": {"decision": 0.86, "answer": 0.14},
                },
                "is_gossip": {"noul": 0.03},
            }
        )
        client = FakeClient([FakeResponse(ollama_payload(content))])
        provider = self._provider(client)
        response = provider.evaluate(
            "text",
            {
                "content_type": Choice(instructions="?", options={"decision": "", "answer": ""}),
                "is_gossip": Noul(instructions="?"),
            },
        )

        assert response.answers["content_type"].choice == "decision"
        assert response.answers["content_type"].confidence == pytest.approx(0.72)
        assert response.answers["is_gossip"].noul == pytest.approx(0.03)

    def test_fenced_json_still_parses(self):
        fenced = "```json\n" + json.dumps({"is_gossip": {"noul": 0.9}}) + "\n```"
        client = FakeClient([FakeResponse(ollama_payload(fenced))])
        provider = self._provider(client)
        response = provider.evaluate("text", {"is_gossip": Noul(instructions="?")})
        assert response.answers["is_gossip"].noul == pytest.approx(0.9)

    def test_unknown_choice_option_and_missing_answers_are_skipped(self):
        content = json.dumps({"content_type": {"choice": "banana", "probabilities": {}}})
        client = FakeClient([FakeResponse(ollama_payload(content))])
        provider = self._provider(client)
        response = provider.evaluate(
            "text",
            {
                "content_type": Choice(instructions="?", options={"decision": "", "chat": ""}),
                "is_gossip": Noul(instructions="?"),
            },
        )
        assert "content_type" not in response.answers
        assert "is_gossip" not in response.answers

    def test_schema_rejection_falls_back_to_json_mode(self):
        content = json.dumps({"is_gossip": {"noul": 0.2}})
        client = FakeClient(
            [FakeResponse({"error": "bad format"}, status_code=400), FakeResponse(ollama_payload(content))]
        )
        provider = self._provider(client)
        response = provider.evaluate("text", {"is_gossip": Noul(instructions="?")})

        assert len(client.calls) == 2
        assert client.calls[1]["json"]["format"] == "json"
        assert response.answers["is_gossip"].noul == pytest.approx(0.2)

    def test_unparseable_response_is_an_error_not_an_answer(self):
        client = FakeClient([FakeResponse(ollama_payload("I think it is probably gossip."))])
        provider = self._provider(client)
        with pytest.raises(ValueError):
            provider.evaluate("text", {"is_gossip": Noul(instructions="?")})


class TestProviderFactory:
    def test_disabled_by_default(self, monkeypatch):
        assert prov_mod.get_decision_provider() is None

    def test_unknown_provider_disables_the_layer(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "magic")
        assert prov_mod.get_decision_provider() is None

    def test_ollama_selected_from_env(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "ollama")
        monkeypatch.setenv("THREADWEAVE_LLM_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setenv("THREADWEAVE_DECISION_MODEL", "qwen3.5:9b")
        provider = prov_mod.get_decision_provider()
        assert isinstance(provider, OllamaDecisionProvider)
        assert provider.model == "qwen3.5:9b"
        assert provider._endpoint() == "http://localhost:11434/api/chat"


# ── TypeSafe seam (privacy-gated) ────────────────────────────


class TestTypeSafeProvider:
    def test_unavailable_without_key_or_remote_optin(self):
        assert TypeSafeDecisionProvider(api_key="k", allow_remote=False).is_available() is False
        assert TypeSafeDecisionProvider(api_key="", allow_remote=True).is_available() is False

    def test_refuses_content_when_remote_is_not_allowed(self):
        provider = TypeSafeDecisionProvider(api_key="k", allow_remote=False)
        with pytest.raises(RemoteContentNotAllowed):
            provider.evaluate("Teams message body", {"is_gossip": Noul(instructions="?")})

    def test_posts_documented_shape_and_parses_documented_answer(self):
        client = FakeClient(
            [
                FakeResponse(
                    {
                        "model": "jev-1.13.0",
                        "answers": {
                            "content_type": {
                                "type": "choice",
                                "choice": "decision",
                                "probabilities": {"decision": 0.88, "answer": 0.12},
                                "confidence": 0.81,
                            },
                            "is_gossip": {"type": "noul", "noul": 0.95},
                        },
                        "usage": {"input_tokens": 296, "output_tokens": 20},
                    }
                )
            ]
        )
        provider = TypeSafeDecisionProvider(api_key="k", allow_remote=True, client=client)
        response = provider.evaluate(
            "state",
            {
                "content_type": Choice(instructions="?", options={"decision": "", "answer": ""}),
                "is_gossip": Noul(instructions="?", true_meaning="gossip"),
            },
        )

        assert client.calls[0]["url"] == prov_mod.TYPESAFE_ENDPOINT
        body = client.calls[0]["json"]
        assert body["model"] == "jev-latest"
        assert body["questions"]["is_gossip"]["type"] == "noul"
        assert body["questions"]["content_type"]["criteria"] == {"decision": None, "answer": None}
        assert response.model == "jev-1.13.0"
        assert response.usage == {"input_tokens": 296, "output_tokens": 20}
        assert response.answers["content_type"].confidence == pytest.approx(0.81)
        assert response.answers["is_gossip"].noul == pytest.approx(0.95)

    def test_remote_optin_via_env(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_ALLOW_REMOTE", "1")
        provider = TypeSafeDecisionProvider(api_key="k")
        assert provider.is_available() is True

    def test_typesafe_selected_from_env(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "typesafe")
        monkeypatch.setenv("THREADWEAVE_DECISION_API_KEY", "k")
        monkeypatch.setenv("THREADWEAVE_DECISION_ALLOW_REMOTE", "yes")
        provider = prov_mod.get_decision_provider()
        assert isinstance(provider, TypeSafeDecisionProvider)
        assert provider.is_available() is True


# ── Gate policy ──────────────────────────────────────────────


DECISION_TEXT = (
    "After three weeks of load testing we decided to move the session cache to "
    "Redis because the Postgres advisory locks were the bottleneck under 2k rps."
)


class TestGatePolicy:
    # Every sample below is longer than the ASCII floor (50 chars) so it
    # routes to the model instead of short-circuiting to regex.
    GOSSIP_TEXT = (
        "Honestly you should hear what I heard about Lars, he is a nightmare "
        "and everyone in the department knows it by now."
    )
    CRITIQUE_TEXT = (
        "The design review was brutal because the plan is weak in three places, "
        "though the team itself did a decent job under pressure."
    )
    PII_TEXT = (
        "Her salary came up in the review and someone pasted the personal "
        "mobile number and home address into the same document."
    )

    @pytest.mark.asyncio
    async def test_confident_decision_is_saved_with_typed_language_and_scope(self):
        gate, engine = gate_for(
            {
                "content_type": choice_answer("decision", 0.88),
                "is_gossip": NoulAnswer(noul=0.04),
                "has_pii": NoulAnswer(noul=0.03),
                "language": choice_answer("no", 0.93),
                "scope": choice_answer("department", 0.77),
            }
        )
        should_save, result = await gate.is_worth_saving(DECISION_TEXT)

        assert should_save is True
        assert result.content_type is ContentType.DECISION
        assert result.language == "no"
        assert result.suggested_scope == "department"
        assert result.has_gossip is False
        assert len(engine.provider.calls) == 1  # one batched call, no escalation

    @pytest.mark.asyncio
    async def test_signals_carry_the_llm_marker_for_detector_mode(self):
        gate, _ = gate_for({"content_type": choice_answer("decision", 0.9)})
        result = await gate.detect(DECISION_TEXT)
        assert any("llm(" in s for s in result.signals)

    @pytest.mark.asyncio
    async def test_gossip_above_the_bar_is_rejected(self):
        gate, _ = gate_for({"is_gossip": NoulAnswer(noul=0.94)})
        result = await gate.detect(self.GOSSIP_TEXT)
        assert result.has_gossip is True
        assert any("gossip_rejected_at" in s for s in result.signals)

    @pytest.mark.asyncio
    async def test_gossip_in_the_uncertain_band_escalates_instead_of_dropping(self):
        escalated = []

        async def escalate(text):
            escalated.append(text)
            fallback = regex_detect(text)
            fallback.has_gossip = False
            fallback.suggested_title = "Work criticism, not gossip"
            return fallback

        gate, _ = gate_for({"is_gossip": NoulAnswer(noul=0.62)}, escalate=escalate)
        result = await gate.detect(self.CRITIQUE_TEXT)

        assert escalated  # second opinion was asked for
        assert result.has_gossip is False
        assert any("gossip_uncertain" in s for s in result.signals)
        assert any("escalated(" in s for s in result.signals)

    @pytest.mark.asyncio
    async def test_gossip_in_the_band_without_escalation_stays_unrejected(self):
        gate, _ = gate_for({"is_gossip": NoulAnswer(noul=0.62)})
        result = await gate.detect(self.CRITIQUE_TEXT)
        assert result.has_gossip is False
        assert any("gossip_uncertain" in s for s in result.signals)

    @pytest.mark.asyncio
    async def test_low_type_confidence_escalates_and_keeps_completion_fields(self):
        async def escalate(text):
            result = regex_detect(text)
            result.content_type = ContentType.ANSWER
            result.confidence = 0.66
            result.suggested_title = "Cache migration rationale"
            result.entities = [{"type": "technology", "value": "Redis"}]
            return result

        gate, _ = gate_for(
            {"content_type": choice_answer("chat", 0.31)}, escalate=escalate
        )
        outcome = await gate.evaluate(DECISION_TEXT)

        assert outcome.escalated is True
        assert outcome.result.content_type is ContentType.ANSWER
        assert outcome.result.confidence == pytest.approx(0.66)
        assert outcome.result.suggested_title == "Cache migration rationale"
        assert outcome.result.entities == [{"type": "technology", "value": "Redis"}]

    @pytest.mark.asyncio
    async def test_low_type_confidence_without_escalation_keeps_the_answer(self):
        gate, _ = gate_for({"content_type": choice_answer("chat", 0.31)})
        outcome = await gate.evaluate(DECISION_TEXT)
        assert outcome.escalated is False
        assert outcome.result.content_type is ContentType.CHAT
        assert outcome.result.confidence == pytest.approx(0.31)

    @pytest.mark.asyncio
    async def test_strong_pii_probability_rejects(self):
        gate, _ = gate_for({"has_pii": NoulAnswer(noul=0.91)})
        result = await gate.detect(
            "His fødselsnummer is 01019012345 and it is written straight into the "
            "meeting notes that get shared with the whole department."
        )
        assert result.has_pii is True

    @pytest.mark.asyncio
    async def test_uncertain_pii_asks_for_a_second_opinion(self):
        async def escalate(text):
            fallback = regex_detect(text)
            fallback.has_pii = True
            return fallback

        gate, _ = gate_for({"has_pii": NoulAnswer(noul=0.6)}, escalate=escalate)
        result = await gate.detect(self.PII_TEXT)
        assert result.has_pii is True
        assert any("pii_uncertain" in s for s in result.signals)

    @pytest.mark.asyncio
    async def test_unknown_language_option_falls_back_to_the_detector(self):
        # "other" means the model could not place it; the deterministic
        # detector still answers, because translation routing needs a language.
        gate, _ = gate_for({"language": choice_answer("other", 0.95)})
        result = await gate.detect(DECISION_TEXT)
        assert result.language == "en"
        assert any(sig.startswith("language_id=") for sig in result.signals)

    @pytest.mark.asyncio
    async def test_low_confidence_language_falls_back_to_the_detector(self):
        gate, _ = gate_for({"language": choice_answer("no", 0.2)})
        result = await gate.detect(DECISION_TEXT)
        assert result.language == "en"  # detector, not the model's weak guess
        assert any(sig.startswith("language_id=") for sig in result.signals)

    @pytest.mark.asyncio
    async def test_provider_that_cannot_answer_language_is_not_asked(self):
        engine = DecisionEngine(FakeProvider({"content_type": choice_answer("decision", 0.9)}))
        engine.provider.answers_language = False
        gate = DecisionGate(engine, policy=DecisionPolicy(), escalate=None)
        assert "language" not in gate.questions
        # give it a Norwegian message so the detector has to do the work
        result = await gate.detect(
            "Etter tre uker med lasttesting besluttet vi å flytte sesjonscachen til "
            "Redis fordi Postgres-låsene ble flaskehalsen ved rundt 2000 forespørsler."
        )
        assert result.language == "no"

    @pytest.mark.asyncio
    async def test_short_text_never_calls_the_model(self):
        gate, engine = gate_for({"content_type": choice_answer("chat", 0.9)})
        outcome = await gate.evaluate("ok thanks")
        assert outcome.reason == "too_short"
        assert engine.provider.calls == []
        assert "too_short" in outcome.result.signals

    @pytest.mark.asyncio
    async def test_provider_failure_falls_back_and_says_so(self):
        engine = DecisionEngine(FakeProvider(error="ollama down"))
        gate = DecisionGate(engine)
        outcome = await gate.evaluate(DECISION_TEXT)
        assert any("decisions_unavailable" in s for s in outcome.result.signals)
        assert outcome.result is not None  # regex result, not an exception

    @pytest.mark.asyncio
    async def test_scope_below_threshold_falls_back_to_team(self):
        gate, _ = gate_for({"scope": choice_answer("organization", 0.3)})
        result = await gate.detect(DECISION_TEXT)
        assert result.suggested_scope == "team"


class TestGateSingleton:
    def test_off_by_default(self, monkeypatch):
        assert gate_mod.get_decision_gate() is None

    def test_active_when_ollama_configured(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "ollama")
        gate = gate_mod.get_decision_gate()
        assert gate is not None
        assert gate.provider_name == "ollama"

    def test_reset_drops_the_cached_gate(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "ollama")
        assert gate_mod.get_decision_gate() is not None
        gate_mod.reset_decision_gate()
        monkeypatch.delenv("THREADWEAVE_DECISION_PROVIDER")
        assert gate_mod.get_decision_gate() is None


# ── Integration with the detector entry points ───────────────


class TestDetectorIntegration:
    @pytest.mark.asyncio
    async def test_detect_async_uses_the_gate_when_configured(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_DECISION_PROVIDER", "ollama")
        gate_mod.reset_decision_gate()

        fake = FakeProvider(
            {
                "content_type": choice_answer("decision", 0.9),
                "language": choice_answer("en", 0.95),
            }
        )
        monkeypatch.setattr(
            "threadweave.decision_gate._default_escalator", lambda: None
        )
        monkeypatch.setattr(
            gate_mod, "get_decision_gate", lambda: DecisionGate(DecisionEngine(fake))
        )

        from threadweave.detector import detect_async

        result = await detect_async(DECISION_TEXT)
        assert result.content_type is ContentType.DECISION
        assert fake.calls  # the typed layer answered it

    @pytest.mark.asyncio
    async def test_detect_async_untouched_when_layer_is_off(self, monkeypatch):
        gate_mod.reset_decision_gate()
        from threadweave.detector import detect_async

        result = await detect_async(DECISION_TEXT)
        # Regex classifier: no "llm(" marker, no decision-layer signal.
        assert not any("decisions/" in s for s in result.signals)

    def test_min_length_contract_matches_the_llm_detector(self):
        from threadweave.llm_detector import LLMDetector

        for text in ("ok thanks", "Vi besluttet å bytte til Postgres", "Short", "æ" * 40):
            assert effective_min_length(text, 50) == LLMDetector._effective_min_length(text, 50)
