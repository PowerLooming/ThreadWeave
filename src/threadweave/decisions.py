# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Typed decision layer — primitives, policy, and audit.

ThreadWeave's ingest gate asks a handful of narrow questions about every
message: is this worth keeping, is it gossip, does it carry PII, what
language is it in, how wide is its scope. Those are judgments, not prose,
and they are currently answered by one monolithic "classify this text"
prompt whose JSON is parsed back with regex fallbacks.

This module makes them what they are: typed decisions the code can branch
on. The three primitives mirror the TypeSafe AI "System One" contract
(https://docs.typesafe.ai/primitives) so a hosted decision model can be
dropped in behind the same interface later:

    Noul   -> probability the statement is true (0.0 - 1.0)
    Choice -> one option from a set, plus probabilities + confidence
    Score  -> a level on an ordered rubric, plus probabilities + confidence

Confidence is derived from the probability distribution the same way
TypeSafe documents it: all mass on one option is 1.0, an even split is 0.0.
That formula lives here rather than in a provider so every backend reports
comparable numbers, and so the policy thresholds mean the same thing no
matter which provider answered.

Policy (thresholds, escalation, what "uncertain" does) lives here in code,
never in a prompt. The provider only answers questions; the code decides
what to do with the answers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

__all__ = [
    "Noul",
    "Choice",
    "Score",
    "Question",
    "Answer",
    "NoulAnswer",
    "ChoiceAnswer",
    "ScoreAnswer",
    "DecisionResponse",
    "DecisionProvider",
    "DecisionPolicy",
    "DecisionAudit",
    "DecisionEngine",
    "QuestionError",
    "confidence_from_probabilities",
    "env_flag",
]


class QuestionError(ValueError):
    """A question is malformed (empty options, duplicate ids, bad id shape)."""


def env_flag(name: str, default: bool = False) -> bool:
    """Truthy env read used across the decision layer (1/true/yes/on)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def confidence_from_probabilities(values: Iterable[float]) -> float:
    """Collapse a probability distribution into a 0-1 confidence.

    Mirrors the derivation TypeSafe documents for Choice/Score answers:
    ``(n * peak - 1) / (n - 1)``, where n is the number of options. All
    mass on one option gives 1.0, an even split gives 0.0.

    A single option is trivially certain (1.0) rather than a division by
    zero. Empty input is 0.0: no distribution means no confidence.
    """
    probs = [max(0.0, float(p)) for p in values]
    n = len(probs)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    total = sum(probs)
    if total <= 0:
        return 0.0
    peak = max(probs) / total
    return max(0.0, min(1.0, (n * peak - 1.0) / (n - 1)))


def normalize_probabilities(
    raw: Mapping[str, Any], keys: Sequence[str]
) -> Dict[str, float]:
    """Coerce a provider's probability map into ``{key: p}`` summing to 1.

    Missing keys get 0.0, negative/garbage values get 0.0, and the result is
    renormalized. If nothing usable came back the distribution is uniform:
    an honest "I have no idea" that scores confidence 0.0 instead of
    inventing certainty.
    """
    clean: Dict[str, float] = {}
    for key in keys:
        value = raw.get(key, 0.0)
        try:
            prob = float(value)
        except (TypeError, ValueError):
            prob = 0.0
        clean[key] = prob if prob > 0 else 0.0
    total = sum(clean.values())
    if total <= 0:
        share = 1.0 / len(keys) if keys else 0.0
        return {key: share for key in keys}
    return {key: value / total for key, value in clean.items()}


# ── Questions ────────────────────────────────────────────────


def _check_question_id(qid: str) -> str:
    qid = (qid or "").strip()
    if not qid:
        raise QuestionError("question id must be non-empty")
    if not qid.replace("_", "").isalnum():
        raise QuestionError(f"question id must be alphanumeric/underscore: {qid!r}")
    return qid


@dataclass(frozen=True)
class Noul:
    """A yes/no question. Answers with the probability that it is true."""

    instructions: str
    true_meaning: str = ""
    false_meaning: str = ""

    type: str = field(default="noul", init=False)

    def criteria(self) -> Dict[str, str]:
        crit: Dict[str, str] = {}
        if self.true_meaning:
            crit["true"] = self.true_meaning
        if self.false_meaning:
            crit["false"] = self.false_meaning
        return crit

    def to_wire(self) -> Dict[str, Any]:
        """TypeSafe-compatible question object."""
        wire: Dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        crit = self.criteria()
        if crit:
            wire["criteria"] = crit
        return wire


@dataclass(frozen=True)
class Choice:
    """Pick one option from a fixed set, with a probability per option."""

    instructions: str
    options: Mapping[str, str]
    type: str = field(default="choice", init=False)

    def __post_init__(self) -> None:
        if not self.options:
            raise QuestionError("Choice needs at least one option")
        if len(self.options) > 255:
            raise QuestionError("Choice accepts at most 255 options")

    def option_keys(self) -> list[str]:
        return list(self.options.keys())

    def to_wire(self) -> Dict[str, Any]:
        return {
            "type": "choice",
            "instructions": self.instructions,
            "criteria": {k: (v if v else None) for k, v in self.options.items()},
        }


@dataclass(frozen=True)
class Score:
    """Rate the state along ordered levels (2-10 of them)."""

    instructions: str
    levels: Sequence[str]
    type: str = field(default="score", init=False)

    def __post_init__(self) -> None:
        if len(self.levels) < 2:
            raise QuestionError("Score needs at least two levels")
        if len(self.levels) > 10:
            raise QuestionError("Score accepts at most 10 levels")

    def level_keys(self) -> list[str]:
        return [str(i) for i in range(len(self.levels))]

    def to_wire(self) -> Dict[str, Any]:
        return {
            "type": "score",
            "instructions": self.instructions,
            "criteria": list(self.levels),
        }


Question = Union[Noul, Choice, Score]


# ── Answers ──────────────────────────────────────────────────


@dataclass
class NoulAnswer:
    """Probability that the statement is true, 0.0 - 1.0.

    TypeSafe does not attach a confidence to Noul answers; the probability
    is the whole answer. ``confidence`` here is therefore the distance from
    a coin flip, ``|2p - 1|``, which is what a policy actually wants when it
    asks "did the model have a view at all".
    """

    noul: float
    type: str = field(default="noul", init=False)

    @property
    def confidence(self) -> float:
        return abs(2.0 * max(0.0, min(1.0, self.noul)) - 1.0)

    @property
    def is_true(self) -> bool:
        return self.noul >= 0.5


@dataclass
class ChoiceAnswer:
    choice: str
    probabilities: Dict[str, float]
    confidence: float
    type: str = field(default="choice", init=False)


@dataclass
class ScoreAnswer:
    score: float
    legend: Dict[str, str]
    probabilities: Dict[str, float]
    confidence: float
    type: str = field(default="score", init=False)


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


# ── Provider interface ───────────────────────────────────────


@dataclass
class DecisionResponse:
    """One batched evaluation: every question answered in a single call."""

    provider: str
    model: str = ""
    answers: Dict[str, Answer] = field(default_factory=dict)
    usage: Dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.answers)


class DecisionProvider:
    """Answers typed questions about a state.

    Implementations must answer ALL questions in one call (that batching is
    the point: one pass, many atomic judgments) and must never invent an
    answer for a question they failed to evaluate — a missing key means
    "no answer", and the policy treats it as such.
    """

    name: str = "abstract"
    model: str = ""

    def is_available(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def evaluate(
        self, state: str, questions: Mapping[str, Question]
    ) -> DecisionResponse:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        """Release transports. Providers without state do nothing."""

    def describe(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name


# ── Policy ───────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", name, raw)
        return default


@dataclass
class DecisionPolicy:
    """Thresholds and escalation rules. Policy lives in code, not prompts.

    Every threshold is a risk decision, so each has its own knob:

    * ``min_confidence``     — below this the primary answer is not trusted
                               and the run escalates (or stays low-confidence).
    * ``gossip_reject_at``   — rejection is destructive (knowledge is lost),
                               so it needs a high bar.
    * ``gossip_review_at``   — between review and reject the content is
                               flagged for a human/detector, not silently
                               dropped.
    * ``pii_reject_at``      — PII false positives reject ingest, so this is
                               deliberately conservative.
    * ``language_min_confidence`` — a language guess only steers translation;
                               a wrong guess costs a re-translation, not data.
    """

    min_confidence: float = 0.55
    gossip_reject_at: float = 0.80
    gossip_review_at: float = 0.50
    pii_reject_at: float = 0.75
    language_min_confidence: float = 0.40

    @classmethod
    def from_env(cls) -> "DecisionPolicy":
        return cls(
            min_confidence=_env_float("THREADWEAVE_DECISION_MIN_CONFIDENCE", 0.55),
            gossip_reject_at=_env_float("THREADWEAVE_DECISION_GOSSIP_REJECT_AT", 0.80),
            gossip_review_at=_env_float("THREADWEAVE_DECISION_GOSSIP_REVIEW_AT", 0.50),
            pii_reject_at=_env_float("THREADWEAVE_DECISION_PII_REJECT_AT", 0.75),
            language_min_confidence=_env_float(
                "THREADWEAVE_DECISION_LANGUAGE_MIN_CONFIDENCE", 0.40
            ),
        )


# ── Audit ────────────────────────────────────────────────────


def default_audit_path() -> Path:
    override = os.environ.get("THREADWEAVE_DECISION_AUDIT")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("THREADWEAVE_HOME") or str(Path.home() / ".threadweave")
    return Path(home).expanduser() / "decision_audit.jsonl"


@dataclass
class DecisionAudit:
    """Append-only decision log (JSONL).

    A typed decision is worth more than a vibe only if you can show what was
    decided and how sure the model was. Every evaluation lands here with a
    content hash and a short excerpt, so a rejected message can be defended
    after the fact without keeping the whole body.

    Audit failures are logged, never raised: losing an audit line must not
    take down an ingest.
    """

    path: Path = field(default_factory=default_audit_path)
    excerpt_chars: int = 120
    store_excerpt: bool = True
    enabled: bool = True

    def record(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        state: str,
        answers: Mapping[str, Answer],
        outcome: str,
        escalated: bool = False,
        latency_ms: float = 0.0,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "outcome": outcome,
            "escalated": escalated,
            "latency_ms": round(latency_ms, 2),
            "content_sha256": hashlib.sha256(state.encode("utf-8")).hexdigest(),
            "content_len": len(state),
            "answers": {
                key: self._answer_dict(answer) for key, answer in answers.items()
            },
        }
        if self.store_excerpt:
            entry["excerpt"] = state[: self.excerpt_chars]
        if extra:
            entry.update(dict(extra))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("decision audit write failed (%s): %s", self.path, exc)
            return None
        return entry

    @staticmethod
    def _answer_dict(answer: Answer) -> Dict[str, Any]:
        if isinstance(answer, NoulAnswer):
            return {"type": "noul", "noul": round(answer.noul, 4)}
        if isinstance(answer, ChoiceAnswer):
            return {
                "type": "choice",
                "choice": answer.choice,
                "confidence": round(answer.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in answer.probabilities.items()},
            }
        if isinstance(answer, ScoreAnswer):
            return {
                "type": "score",
                "score": round(answer.score, 4),
                "confidence": round(answer.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in answer.probabilities.items()},
            }
        return {"type": "unknown"}  # pragma: no cover - closed union


# ── Engine ───────────────────────────────────────────────────


class DecisionEngine:
    """One provider + one policy + one audit log.

    The engine owns the batching rule: every question for one state goes out
    in a single call, and the answers come back keyed by question id.
    """

    def __init__(
        self,
        provider: DecisionProvider,
        policy: Optional[DecisionPolicy] = None,
        audit: Optional[DecisionAudit] = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or DecisionPolicy.from_env()
        self.audit = audit if audit is not None else DecisionAudit()

    @property
    def available(self) -> bool:
        try:
            return bool(self.provider.is_available())
        except Exception as exc:  # a provider probe must never break ingest
            logger.debug("provider %s availability probe failed: %s", self.provider, exc)
            return False

    def ask(
        self,
        state: str,
        questions: Mapping[str, Question],
        *,
        purpose: str = "decision",
        audit_extra: Optional[Mapping[str, Any]] = None,
    ) -> DecisionResponse:
        """Evaluate every question against ``state`` in one call."""
        if not questions:
            raise QuestionError("no questions to ask")
        for qid in questions:
            _check_question_id(qid)

        started = time.perf_counter()
        try:
            response = self.provider.evaluate(state, questions)
        except Exception as exc:  # provider faults degrade, never crash ingest
            logger.warning("decision provider %s failed: %s", self.provider.describe(), exc)
            response = DecisionResponse(
                provider=self.provider.name,
                model=self.provider.model,
                error=str(exc),
            )
        response.latency_ms = response.latency_ms or (
            (time.perf_counter() - started) * 1000.0
        )
        self.audit.record(
            purpose=purpose,
            provider=response.provider,
            model=response.model,
            state=state,
            answers=response.answers,
            outcome="ok" if response.ok else f"error:{response.error[:80]}",
            latency_ms=response.latency_ms,
            extra=audit_extra,
        )
        return response
