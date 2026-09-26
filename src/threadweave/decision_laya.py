# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Laya decision provider — typed decisions from a local non-autoregressive model.

Laya (``pip install laya``, Apache-2.0, Convai Innovations) answers ``choice``,
``score`` and ``noul`` questions in a single forward pass and returns typed
answers with probabilities. That is the same contract this layer speaks, so the
adapter is mostly a translation table with two fixes on top, both of which come
from measurements on ThreadWeave's own ingest questions rather than from the
model card:

1. **Checkpoints are chosen by our own language detector, not by Laya's
   router.** Measured: Norwegian and Danish text was routed to the English
   checkpoint (the Norwegian sample was detected as French), and the same gossip
   sentence scored 0.239 as gossip on the English checkpoint against 0.949 on
   the multilingual one. Non-English text must reach the multilingual
   checkpoint, so ``language_id.identify`` decides here, and
   ``THREADWEAVE_DECISION_LAYA_MODEL`` can pin one checkpoint instead.

2. **Noul questions are asked as a two-option choice by default.** Measured on a
   24-item set: the native ``noul`` form reported a mean probability of 0.322
   on true statements against 0.009 on false ones, while the two-option
   ``choice`` form reached 0.539 against 0.223 with a better ECE. This matches
   the open issue on Laya's tracker (#156, "noul gets stuck"), where the
   recommended workaround is exactly this. ``THREADWEAVE_DECISION_LAYA_NOUL_FORM
   =noul`` restores the native form.

Asked-for-answers only: a question Laya does not answer is left out of the
response rather than filled in with a guess.

Context on the competition, measured on the same 24 items (gossip question, mean
probability on true against false, ECE in brackets): Laya typed-decisions
checkpoint via two-option choice 0.539 / 0.223 (0.171), the local NLI encoder
0.441 / 0.030 (0.279). Laya is much faster (0.05 s per message for four
questions against 0.25 s for two) and is not clearly more accurate, so this
provider is opt-in rather than the default.

Optional dependency (never imported at module load):
    uv pip install laya
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Mapping, Optional

from threadweave.decisions import (
    Answer,
    Choice,
    ChoiceAnswer,
    DecisionProvider,
    DecisionResponse,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
    confidence_from_probabilities,
    normalize_probabilities,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LayaDecisionProvider",
    "LayaUnavailable",
    "CHECKPOINT_ENGLISH",
    "CHECKPOINT_MULTILINGUAL",
    "CHECKPOINT_TYPED_DECISIONS",
]

CHECKPOINT_ENGLISH = "english"
CHECKPOINT_MULTILINGUAL = "multilingual"
CHECKPOINT_TYPED_DECISIONS = "typed-decisions"

INSTALL_HINT = "laya provider needs the laya package: uv pip install laya"

# Neutral option keys for the two-option Noul form. Their own guidance is to use
# neutral keys and put the yes/no wording in the descriptions, so the key token
# itself carries no bias.
_NOUL_TRUE_KEY = "A"
_NOUL_FALSE_KEY = "B"


class LayaUnavailable(RuntimeError):
    """The laya package is missing, or no checkpoint could be selected."""


class LayaDecisionProvider(DecisionProvider):
    """Answers typed questions with a local Laya checkpoint.

    One ``predict`` call answers every question, so cost does not grow with the
    number of questions the way it does for the encoder provider. The router is
    built lazily on the first evaluation and reused, because loading weights is
    the slow part.
    """

    name = "laya"
    # Their router misroutes Latin-script languages that are close to English
    # (measured with Norwegian and Danish), so language is not asked here and
    # the gate answers it with language_id.py instead.
    answers_language = False

    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[str] = None,
        checkpoint_en: str = CHECKPOINT_ENGLISH,
        checkpoint_other: str = CHECKPOINT_MULTILINGUAL,
        preload: bool = False,
        noul_form: str = "choice",
        router: Optional[Any] = None,
        router_factory: Optional[Callable[[], Any]] = None,
        language_detector: Optional[Callable[[str], Any]] = None,
    ) -> None:
        # None or empty means "route per message"; anything else pins one
        # checkpoint for every message.
        self.model = (model or "").strip()
        self.device = device
        self.checkpoint_en = checkpoint_en
        self.checkpoint_other = checkpoint_other
        self.preload = preload
        self.noul_form = (noul_form or "choice").strip().lower()
        self._router = router
        self._router_factory = router_factory
        self._detector = language_detector
        self.calls = 0
        self.last_latency_ms = 0.0
        self.last_checkpoints: list[str] = []

    # ── availability ─────────────────────────────────────────

    def is_available(self) -> bool:
        """True when the package can be imported. Never loads weights.

        This runs on every health check, so a weight load here would be absurd.
        """
        if self._router is not None:
            return True
        try:
            import laya  # noqa: F401
        except ImportError:
            return False
        return True

    def close(self) -> None:
        self._router = None

    # ── router plumbing ──────────────────────────────────────

    def _resolve_device(self) -> str:
        if self.device:
            return self.device
        env = (os.environ.get("THREADWEAVE_DECISION_LAYA_DEVICE") or "").strip().lower()
        if env:
            return env
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:  # pragma: no cover - guarded by is_available
            return "cpu"

    def _get_router(self) -> Any:
        if self._router is not None:
            return self._router
        if self._router_factory is not None:
            try:
                self._router = self._router_factory()
            except ImportError as exc:
                raise LayaUnavailable(INSTALL_HINT) from exc
            return self._router
        try:
            from laya import Router
        except ImportError as exc:
            raise LayaUnavailable(INSTALL_HINT) from exc
        started = time.perf_counter()
        self._router = Router(
            device=self._resolve_device(),
            preload=self.preload or bool(self.model),
        )
        logger.info(
            "laya router loaded in %.1fs (device=%s, preload=%s)",
            time.perf_counter() - started,
            self._resolve_device(),
            self.preload or bool(self.model),
        )
        return self._router

    # ── checkpoint selection ─────────────────────────────────

    def _detect_language(self, state: str) -> str:
        if self._detector is not None:
            guess = self._detector(state)
            return getattr(guess, "language", "") or ""

        from threadweave.language_id import identify

        return identify(state).language or ""

    def _checkpoint_for(self, state: str) -> str:
        """One checkpoint per message, chosen by our language detector.

        Unknown or ambiguous language goes to the multilingual checkpoint: an
        English message read by the multilingual model is degraded but usable,
        while non-English text read by the English checkpoint collapses (0.098
        mean probability on true gossip statements against 0.369).
        """
        if self.model:
            return self.model
        language = self._detect_language(state)
        if language == "en":
            return self.checkpoint_en
        return self.checkpoint_other

    # ── question translation ─────────────────────────────────

    def _to_laya(self, question: Question) -> Dict[str, Any]:
        if isinstance(question, Noul):
            if self.noul_form == "noul" or not (
                question.true_meaning and question.false_meaning
            ):
                return {
                    "type": "noul",
                    "instructions": question.instructions or question.statement,
                }
            return {
                "type": "choice",
                "instructions": question.instructions or question.statement,
                "criteria": {
                    _NOUL_TRUE_KEY: question.true_meaning,
                    _NOUL_FALSE_KEY: question.false_meaning,
                },
            }
        if isinstance(question, Choice):
            return {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": {
                    key: (description or key)
                    for key, description in question.options.items()
                },
            }
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.levels),
        }

    def _from_laya(self, question: Question, answer: Mapping[str, Any]) -> Optional[Answer]:
        """Map one Laya answer onto our answer types.

        Confidence is recomputed from the probabilities rather than taken from
        Laya's ``confidence`` or ``answer_confidence`` fields, because their
        scale differs (a noul answer of 0.002 comes back with confidence 0.998)
        and mixing scales inside one layer is how thresholds stop meaning
        anything.
        """
        if not isinstance(answer, Mapping):
            return None

        if isinstance(question, Noul):
            if self.noul_form == "noul" or not (
                question.true_meaning and question.false_meaning
            ):
                value = answer.get("noul")
                if value is None:
                    return None
                return NoulAnswer(noul=max(0.0, min(1.0, float(value))))
            probabilities = answer.get("probabilities") or {}
            value = probabilities.get(_NOUL_TRUE_KEY)
            if value is None:
                return None
            return NoulAnswer(noul=max(0.0, min(1.0, float(value))))

        if isinstance(question, Choice):
            raw = answer.get("probabilities") or {}
            keys = question.option_keys()
            mapped = {key: float(raw.get(key, 0.0)) for key in keys}
            if not any(mapped.values()):
                # A single-key answer can come back without a distribution;
                # fall back to the chosen key so a decision is not lost.
                chosen = answer.get("choice")
                if chosen in mapped:
                    mapped = {key: (1.0 if key == chosen else 0.0) for key in keys}
                else:
                    return None
            probabilities = normalize_probabilities(mapped, keys)
            return ChoiceAnswer(
                choice=max(probabilities, key=lambda key: probabilities[key]),
                probabilities=probabilities,
                confidence=confidence_from_probabilities(probabilities.values()),
            )

        raw = answer.get("probabilities") or {}
        keys = question.level_keys()
        mapped = {key: float(raw.get(key, 0.0)) for key in keys}
        if not any(mapped.values()):
            return None
        probabilities = normalize_probabilities(mapped, keys)
        weighted = sum(int(key) * prob for key, prob in probabilities.items())
        return ScoreAnswer(
            score=float(answer.get("score", weighted)),
            legend={str(idx): level for idx, level in enumerate(question.levels)},
            probabilities=probabilities,
            confidence=confidence_from_probabilities(probabilities.values()),
        )

    # ── evaluation ───────────────────────────────────────────

    def evaluate(self, state: str, questions: Mapping[str, Question]) -> DecisionResponse:
        """One call for every question. A failed question only loses itself."""
        started = time.perf_counter()
        checkpoint = self._checkpoint_for(state)
        wire = {qid: self._to_laya(question) for qid, question in questions.items()}
        try:
            router = self._get_router()
            result = router.predict(state, wire, model=checkpoint)
        except LayaUnavailable:
            raise
        except Exception as exc:
            self.last_latency_ms = (time.perf_counter() - started) * 1000.0
            logger.warning("laya evaluation failed: %s", exc)
            return DecisionResponse(
                provider=self.name,
                model=checkpoint,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=self.last_latency_ms,
            )

        self.calls += 1
        answers: Dict[str, Answer] = {}
        raw_answers = (result or {}).get("answers") or {}
        for qid, question in questions.items():
            raw = raw_answers.get(qid)
            if raw is None:
                continue
            try:
                mapped = self._from_laya(question, raw)
            except Exception as exc:  # one bad answer must not lose the rest
                logger.warning("laya answer %s failed to map: %s", qid, exc)
                continue
            if mapped is not None:
                answers[qid] = mapped

        self.last_checkpoints.append(checkpoint)
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        usage = (result or {}).get("usage") or {}
        return DecisionResponse(
            provider=self.name,
            model=checkpoint,
            answers=answers,
            usage={
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
            latency_ms=self.last_latency_ms,
        )
