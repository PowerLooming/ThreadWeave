# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
The ingest decision gate — typed questions in, one DetectionResult out.

This is the adapter between the typed decision layer and the rest of the
pipeline. It replaces the "one fat prompt, then parse the JSON" detector for
everything that is a *judgment* about a message:

    content_type  Choice  answer / decision / question / chat / reference
    is_gossip     Noul    is this about a person rather than the work
    has_pii       Noul    does this carry personal data
    language      Choice  which language the message is in
    scope         Choice  team / department / organization

All five ride in ONE provider call. Generative work (titles, entities) is
deliberately NOT asked as a decision: it comes from the escalation engine
(the existing LLM detector) or, when no LLM is configured, from the regex
classifier — the same engine the pipeline uses today.

Policy is enforced here, in code:

* A judgment at or above its threshold is acted on.
* Gossip and PII have a high bar for rejection and an uncertain band below
  it. Rejecting content destroys knowledge, so the band escalates for a
  second opinion instead of dropping the message on a 0.6 probability.
* An uncertain content type escalates when an escalation engine exists;
  otherwise the typed answer stands with its low confidence and the caller's
  threshold logic (unchanged) decides.

Kill switch: with ``THREADWEAVE_DECISION_PROVIDER`` unset the gate returns
None and the pipeline keeps using ``llm_detector`` / regex exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Union

from threadweave.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionEngine,
    DecisionProvider,
    DecisionPolicy,
    DecisionResponse,
    Noul,
    NoulAnswer,
    Question,
    Score,
)
from threadweave.detector import (
    ContentType,
    DetectionResult,
    detect as regex_detect,
)

logger = logging.getLogger(__name__)

__all__ = [
    "INGEST_QUESTION_IDS",
    "build_ingest_questions",
    "effective_min_length",
    "DecisionGate",
    "get_decision_gate",
    "reset_decision_gate",
]

INGEST_QUESTION_IDS = (
    "content_type",
    "is_gossip",
    "has_pii",
    "language",
    "scope",
)

# Languages the pilot actually sees, plus an escape hatch. A Choice needs a
# closed set; "other" keeps a genuinely unknown language from being forced
# into a wrong bucket (and translation is driven by the code, not the label).
LANGUAGE_OPTIONS: Dict[str, str] = {
    "en": "English",
    "no": "Norwegian (bokmål or nynorsk)",
    "sv": "Swedish",
    "da": "Danish",
    "de": "German",
    "nl": "Dutch",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "fi": "Finnish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "other": "a language not listed above",
}

CONTENT_TYPE_OPTIONS: Dict[str, str] = {
    "answer": (
        "Durable explanation, instruction, convention or best practice that a "
        "colleague would want later"
    ),
    "decision": (
        "A choice that was made, an approval or rejection, with the reasoning "
        "behind it"
    ),
    "question": "Someone asking something, with no durable answer in the same message",
    "chat": "Small talk, greetings, acknowledgements, scheduling noise",
    "reference": (
        "Pointers only: links, ticket numbers, file names, newsletters, "
        "marketing or automated notifications"
    ),
}

TYPE_BY_ID = {
    "answer": ContentType.ANSWER,
    "decision": ContentType.DECISION,
    "question": ContentType.QUESTION,
    "chat": ContentType.CHAT,
    "reference": ContentType.REFERENCE,
}

# A signal must carry this marker for api.py to report detector_mode="llm"
# (it sniffs signals for "llm("). Keep the prefix even as the wording evolves.
LLM_SIGNAL_PREFIX = "llm("


def effective_min_length(text: str, min_length: int = 50) -> int:
    """Language-aware length floor for model routing.

    Contract with ``llm_detector.LLMDetector._effective_min_length``: the
    regex classifier is English-only, so any non-ASCII text gets a low floor
    (a 38-char Norwegian gossip sentence must reach the model, not be
    classified as chat by regex). Pure ASCII keeps the caller's floor to stay
    cheap on "ok" / "thanks".
    """
    if any(ord(ch) > 0x7F for ch in text):
        return 10
    return max(min_length, 50)


def build_ingest_questions(
    *, include_language: bool = True, include_scope: bool = True
) -> Dict[str, Question]:
    """The standard ingest question set, all answered in one call."""
    questions: Dict[str, Question] = {
        "content_type": Choice(
            instructions=(
                "What is this message, judged by the durable knowledge it carries "
                "for the organization?"
            ),
            options=CONTENT_TYPE_OPTIONS,
        ),
        "is_gossip": Noul(
            instructions=(
                "Is this message about a person rather than about the work: "
                "an insult, mockery, backtalk, hearsay about someone, or "
                "relationship gossip?"
            ),
            true_meaning="Targets or gossips about a person",
            false_meaning=(
                "Criticises work, systems, code, deliverables or the situation "
                "itself, which is normal professional communication"
            ),
            statement=(
                "The message contains gossip or an insult about a person."
            ),
        ),
        "has_pii": Noul(
            instructions=(
                "Does this message contain personal data that must not be stored: "
                "personal phone numbers, national ID numbers, credit-card or bank "
                "numbers, passport numbers, home addresses, or an individual's salary?"
            ),
            true_meaning="Contains personal data of that kind",
            false_meaning=(
                "Company and brand names, work email addresses, office addresses, "
                "public organisation numbers, roles and titles, or professional "
                "names in a work context"
            ),
            statement=(
                "The message contains personal data such as a national identity "
                "number or a private phone number."
            ),
        ),
    }
    if include_language:
        questions["language"] = Choice(
            instructions="Which language is this message written in?",
            options=LANGUAGE_OPTIONS,
        )
    if include_scope:
        questions["scope"] = Choice(
            instructions=(
                "How widely does this knowledge apply: one team's own workflow and "
                "tools, several teams inside a department, or the whole organization?"
            ),
            options={
                "team": "Specific to one team's workflow, tools or conventions",
                "department": "Relevant across teams within one department",
                "organization": "A company-wide policy, standard or practice",
            },
        )
    return questions


# ── Gate ─────────────────────────────────────────────────────

# Escalation returns a full DetectionResult (the existing LLM detector or the
# regex classifier), used for generative fields and for second opinions.
Escalator = Callable[[str], Union[DetectionResult, Awaitable[DetectionResult]]]


@dataclass
class GateOutcome:
    """What the gate decided, with the evidence needed to explain it."""

    result: DetectionResult
    response: DecisionResponse
    escalated: bool
    reason: str = ""


class DecisionGate:
    """Answers the ingest questions as typed decisions and applies policy."""

    def __init__(
        self,
        engine: DecisionEngine,
        escalate: Optional[Escalator] = None,
        policy: Optional[DecisionPolicy] = None,
        *,
        include_language: bool = True,
        include_scope: bool = True,
    ) -> None:
        self.engine = engine
        self.policy = policy or engine.policy
        self.escalate = escalate
        # A provider that cannot answer the language question is not asked:
        # the deterministic detector in language_id.py answers it instead.
        self.answers_language = bool(getattr(engine.provider, "answers_language", True))
        self.include_language = include_language and self.answers_language
        self.include_scope = include_scope
        self.questions = build_ingest_questions(
            include_language=self.include_language, include_scope=include_scope
        )

    @property
    def available(self) -> bool:
        return self.engine.available

    @property
    def provider_name(self) -> str:
        return self.engine.provider.name

    # ── public API ───────────────────────────────────────────

    async def detect(self, text: str, min_length: int = 50) -> DetectionResult:
        outcome = await self.evaluate(text, min_length=min_length)
        return outcome.result

    async def is_worth_saving(
        self, text: str, threshold: float = 0.40
    ) -> tuple[bool, DetectionResult]:
        result = await self.detect(text)
        should = (
            result.content_type in (ContentType.ANSWER, ContentType.DECISION)
            and result.confidence >= threshold
        )
        return should, result

    async def evaluate(self, text: str, min_length: int = 50) -> GateOutcome:
        if len(text) < effective_min_length(text, min_length):
            return GateOutcome(
                result=regex_detect(text, min_length),
                response=DecisionResponse(provider=self.provider_name, error="too_short"),
                escalated=False,
                reason="too_short",
            )

        # Providers are synchronous (easy to fake, no loop juggling); run the
        # HTTP call off the event loop so a slow model cannot stall uvicorn.
        response = await asyncio.to_thread(
            self.engine.ask,
            text,
            self.questions,
            purpose="ingest",
            audit_extra={"min_length": min_length},
        )

        if not response.ok:
            fallback = await self._run_escalation(text, min_length)
            fallback.signals.append(f"decisions_unavailable:{response.error[:60]}")
            return GateOutcome(
                result=fallback,
                response=response,
                escalated=True,
                reason="provider_error",
            )

        return await self._apply_policy(text, min_length, response)

    # ── policy ───────────────────────────────────────────────

    async def _apply_policy(
        self, text: str, min_length: int, response: DecisionResponse
    ) -> GateOutcome:
        answers = response.answers
        policy = self.policy
        signals: list[str] = []
        escalated = False
        reasons: list[str] = []

        type_answer = self._choice(answers.get("content_type"))
        content_type = (
            TYPE_BY_ID.get(type_answer.choice, ContentType.CHAT) if type_answer else ContentType.CHAT
        )
        confidence = type_answer.confidence if type_answer else 0.0

        gossip_answer = self._noul(answers.get("is_gossip"))
        pii_answer = self._noul(answers.get("has_pii"))
        lang_answer = self._choice(answers.get("language")) if self.include_language else None
        scope_answer = self._choice(answers.get("scope")) if self.include_scope else None

        signals.append(
            "{prefix}decisions/{provider}): type={ctype}@{tconf:.2f} "
            "gossip={gossip:.2f} pii={pii:.2f}{lang}{scope}".format(
                prefix=LLM_SIGNAL_PREFIX,
                provider=response.provider,
                ctype=content_type.value,
                tconf=confidence,
                gossip=(gossip_answer.noul if gossip_answer else -1.0),
                pii=(pii_answer.noul if pii_answer else -1.0),
                lang=(
                    f" lang={lang_answer.choice}@{lang_answer.confidence:.2f}"
                    if lang_answer
                    else ""
                ),
                scope=(
                    f" scope={scope_answer.choice}@{scope_answer.confidence:.2f}"
                    if scope_answer
                    else ""
                ),
            )
        )

        # Gossip — high bar for a destructive rejection, uncertain band
        # escalates rather than dropping knowledge on a coin-flip.
        has_gossip = False
        if gossip_answer is not None:
            if gossip_answer.noul >= policy.gossip_reject_at:
                has_gossip = True
                signals.append(f"gossip_rejected_at={gossip_answer.noul:.2f}")
            elif gossip_answer.noul >= policy.gossip_review_at:
                signals.append(f"gossip_uncertain={gossip_answer.noul:.2f}")

        has_pii = False
        if pii_answer is not None:
            if pii_answer.noul >= policy.pii_reject_at:
                has_pii = True
                signals.append(f"pii_rejected_at={pii_answer.noul:.2f}")
            elif pii_answer.noul >= policy.gossip_review_at:
                signals.append(f"pii_uncertain={pii_answer.noul:.2f}")

        language = ""
        if lang_answer is not None and lang_answer.confidence >= policy.language_min_confidence:
            language = "" if lang_answer.choice == "other" else lang_answer.choice
        if not language:
            language, lang_signal = self._detect_language(text)
            if lang_signal:
                signals.append(lang_signal)

        scope = "team"
        if scope_answer is not None and scope_answer.confidence >= policy.min_confidence:
            scope = scope_answer.choice

        result = DetectionResult(
            content_type=content_type,
            confidence=confidence,
            signals=signals,
            suggested_scope=scope,
            has_pii=has_pii,
            has_gossip=has_gossip,
            language=language,
        )

        needs_second_opinion = (
            confidence < policy.min_confidence
            or any("gossip_uncertain" in s for s in signals)
            or any("pii_uncertain" in s for s in signals)
        )

        if needs_second_opinion and self.escalate is not None:
            escalated_result = await self._run_escalation(text, min_length)
            escalated = True
            if confidence < policy.min_confidence:
                reasons.append("low_type_confidence")
            if any("gossip_uncertain" in s for s in signals):
                reasons.append("gossip_band")
            if any("pii_uncertain" in s for s in signals):
                reasons.append("pii_band")
            result = self._merge(result, escalated_result, confidence, signals, reasons)

        return GateOutcome(
            result=result,
            response=response,
            escalated=escalated,
            reason=",".join(reasons),
        )

    def _merge(
        self,
        primary: DetectionResult,
        second: DetectionResult,
        type_confidence: float,
        signals: list[str],
        reasons: list[str],
    ) -> DetectionResult:
        """Typed judgments win; the LLM fills in what only generation gives.

        * content type/confidence: the second opinion replaces a low-confidence
          typed answer (that is why we escalated).
        * gossip/PII: only the uncertain band is re-judged; a decisive typed
          answer stands.
        * title/entities: always taken from the second opinion, because the
          gate never asks the model to write anything.
        """
        merged = DetectionResult(
            content_type=(
                second.content_type
                if type_confidence < self.policy.min_confidence
                else primary.content_type
            ),
            confidence=(
                second.confidence
                if type_confidence < self.policy.min_confidence
                else primary.confidence
            ),
            signals=list(primary.signals)
            + [f"escalated({','.join(reasons)})"]
            + [f"escalation: {s}" for s in second.signals[:3]],
            entities=list(second.entities),
            suggested_scope=primary.suggested_scope,
            suggested_title=second.suggested_title,
            has_pii=primary.has_pii,
            has_gossip=primary.has_gossip,
            language=primary.language or second.language,
        )
        if any("gossip_uncertain" in s for s in signals):
            merged.has_gossip = second.has_gossip
        if any("pii_uncertain" in s for s in signals):
            merged.has_pii = second.has_pii
        return merged

    async def _run_escalation(self, text: str, min_length: int) -> DetectionResult:
        """Second opinion from the escalation engine (LLM detector or regex)."""
        if self.escalate is None:
            return regex_detect(text, min_length)
        try:
            outcome = self.escalate(text)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            return outcome
        except Exception as exc:  # escalation must never break the gate
            logger.warning("escalation failed: %s", exc)
            return regex_detect(text, min_length)

    def _detect_language(self, text: str) -> tuple[str, str]:
        """Deterministic language fallback, used when the provider did not answer.

        Returns ``(language, signal)``; an empty language means the guess was
        below the threshold, and the caller then leaves translation alone.
        """
        try:
            from threadweave.language_id import identify

            guess = identify(
                text, min_confidence=self.policy.language_id_min_confidence
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("language detector failed: %s", exc)
            return "", ""
        if not guess.language:
            return "", ""
        return guess.language, f"language_id={guess.language}@{guess.confidence:.2f}"

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _choice(answer: Optional[Any]) -> Optional[ChoiceAnswer]:
        return answer if isinstance(answer, ChoiceAnswer) else None

    @staticmethod
    def _noul(answer: Optional[Any]) -> Optional[NoulAnswer]:
        return answer if isinstance(answer, NoulAnswer) else None


# ── Singleton (mirrors llm_detector.get_llm_detector) ────────

_gate: Optional[DecisionGate] = None
_gate_configured = False


def _default_escalator() -> Optional[Escalator]:
    """Use the existing LLM detector for generative fields, when configured."""
    try:
        from threadweave.llm_detector import get_llm_detector

        detector = get_llm_detector()
    except Exception:  # pragma: no cover - import/env problems degrade to regex
        detector = None
    if detector is None:
        return None

    async def _escalate(text: str) -> DetectionResult:
        return await detector.detect(text)

    return _escalate


def get_decision_gate() -> Optional[DecisionGate]:
    """The configured gate, or None when the typed layer is switched off."""
    global _gate, _gate_configured
    if _gate_configured:
        return _gate
    _gate_configured = True

    from threadweave.decision_providers import get_decision_provider

    provider: Optional[DecisionProvider] = get_decision_provider()
    if provider is None:
        _gate = None
        return None

    policy = DecisionPolicy.from_env()
    engine = DecisionEngine(provider, policy=policy)
    _gate = DecisionGate(engine, policy=policy, escalate=_default_escalator())
    logger.info(
        "typed decision layer active: provider=%s policy(min_conf=%.2f gossip_reject=%.2f)",
        provider.describe(),
        policy.min_confidence,
        policy.gossip_reject_at,
    )
    return _gate


def reset_decision_gate() -> None:
    """Drop the cached gate AND the cached provider.

    Both caches are process-global and both are derived from env vars, so a
    reset that only cleared the gate would rebuild it around the provider
    chosen before the env changed. Tests and config reloads need the whole
    layer re-resolved.
    """
    global _gate, _gate_configured
    _gate = None
    _gate_configured = False
    from threadweave.decision_providers import reset_decision_provider

    reset_decision_provider()
