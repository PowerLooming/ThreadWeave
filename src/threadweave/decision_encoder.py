# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Encoder decision provider — the three primitives from a small NLI classifier.

A generative model asked "how sure are you" answers with its own opinion. An
encoder trained for natural-language inference answers with a probability
distribution computed from logits, which is what the decision layer actually
wants. This provider maps the HF ``zero-shot-classification`` pipeline onto the
same Choice / Noul / Score contract the rest of the layer speaks:

* Choice  -> one call, ``multi_label=False``, candidate labels are the options.
             The pipeline softmaxes the entailment logits across the labels, so
             the scores sum to 1 and are directly usable as ``probabilities``.
* Noul    -> one call, ``multi_label=True``, a single candidate statement. The
             pipeline reduces entailment against contradiction for that one
             label, giving the probability that the statement is true.
* Score   -> one call per level set, ``multi_label=True``: every level is judged
             on its own against the message, exactly as the System One contract
             describes, then the per-level probabilities are normalized and the
             score is the probability-weighted mean level.

Cost is honest and structural: the message is re-encoded once per question, so
token volume (not request count) is the limit. We could pack several questions
into one call, but that would softmax across questions instead of across the
options inside a question, and the probabilities would stop meaning anything.

Phrasing matters. NLI hypotheses are declarative statements, so questions carry
an optional ``statement`` form (see ``Noul.statement``) and option/level text is
wrapped in a template. ``Score`` is the weakest of the three: it depends on the
levels being mutually exclusive, positively phrased statements, and it needs
calibration before its thresholds mean anything.

Optional dependency (never imported at module load):
    uv pip install transformers torch
For a CPU-only install:
    uv pip install --index-url https://download.pytorch.org/whl/cpu torch
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
    "EncoderDecisionProvider",
    "EncoderUnavailable",
    "DEFAULT_ENCODER_MODEL",
    "DEFAULT_CHOICE_TEMPLATE",
    "DEFAULT_STATEMENT_TEMPLATE",
]

# Multilingual and commercial-only training data, so it covers the Norwegian
# share without a second model. English-only corpora do better (and faster)
# with MoritzLaurer/ModernBERT-base-zeroshot-v2.0 or
# MoritzLaurer/deberta-v3-large-zeroshot-v2.0-c.
DEFAULT_ENCODER_MODEL = "MoritzLaurer/bge-m3-zeroshot-v2.0-c"

# The {} is the label. Options and levels are descriptions, so the hypothesis
# reads as "This message is <description>.". A bare option id would read as
# "This message is decision.", which the model handles poorly.
DEFAULT_CHOICE_TEMPLATE = "This message is {}."
# Statements are already declarations, so they are passed through unchanged.
DEFAULT_STATEMENT_TEMPLATE = "{}"

INSTALL_HINT = (
    "encoder provider needs transformers and torch: "
    "uv pip install transformers torch  (CPU-only torch: "
    "uv pip install --index-url https://download.pytorch.org/whl/cpu torch)"
)


class EncoderUnavailable(RuntimeError):
    """transformers/torch are missing, or no model is configured."""


class EncoderDecisionProvider(DecisionProvider):
    """Answers typed questions with a local NLI encoder classifier.

    The pipeline is built lazily on the first evaluation and reused, because
    loading weights is the slow part (the same reason a request-scoped model is
    a bad idea). Pass ``pipeline`` to inject one for tests.
    """

    name = "encoder"
    # NLI cannot identify a language (measured, see the module docstring), so
    # the gate does not ask this provider and uses language_id.py instead.
    answers_language = False

    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[int] = None,
        choice_template: str = DEFAULT_CHOICE_TEMPLATE,
        statement_template: str = DEFAULT_STATEMENT_TEMPLATE,
        pipeline: Optional[Callable[..., Dict[str, Any]]] = None,
        pipeline_factory: Optional[Callable[[], Callable[..., Dict[str, Any]]]] = None,
    ) -> None:
        # None means "use the default model"; an explicit empty string means
        # "no model", which is what makes is_available() honest.
        self.model = DEFAULT_ENCODER_MODEL if model is None else model.strip()
        self.device = device
        self.choice_template = choice_template
        self.statement_template = statement_template
        self._pipeline = pipeline
        self._pipeline_factory = pipeline_factory
        self.calls = 0
        self.last_latency_ms = 0.0

    # ── availability ─────────────────────────────────────────

    def is_available(self) -> bool:
        """True when a model is named and the runtime can be imported.

        Deliberately does NOT build the pipeline: this runs on every health
        check and a weight load there would be absurd.
        """
        if not self.model or self._pipeline is not None:
            return bool(self.model)
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            return False
        return True

    def close(self) -> None:
        self._pipeline = None

    # ── pipeline plumbing ────────────────────────────────────

    def _resolve_device(self) -> int:
        if self.device is not None:
            return self.device
        env = (os.environ.get("THREADWEAVE_DECISION_ENCODER_DEVICE") or "").strip().lower()
        if env == "cpu":
            return -1
        if env.isdigit():
            return int(env)
        try:
            import torch

            return 0 if torch.cuda.is_available() else -1
        except ImportError:  # pragma: no cover - guarded by is_available
            return -1

    def _get_pipeline(self) -> Callable[..., Dict[str, Any]]:
        if self._pipeline is not None:
            return self._pipeline
        if self._pipeline_factory is not None:
            try:
                self._pipeline = self._pipeline_factory()
            except ImportError as exc:
                raise EncoderUnavailable(INSTALL_HINT) from exc
            return self._pipeline
        if not self.model:
            raise EncoderUnavailable("no encoder model configured")
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise EncoderUnavailable(INSTALL_HINT) from exc
        started = time.perf_counter()
        self._pipeline = pipeline(
            "zero-shot-classification",
            model=self.model,
            device=self._resolve_device(),
        )
        logger.info(
            "encoder model %s loaded in %.1fs (device=%s)",
            self.model,
            time.perf_counter() - started,
            self._resolve_device(),
        )
        return self._pipeline

    def _run(
        self, state: str, labels: list[str], template: str, multi_label: bool
    ) -> Dict[str, float]:
        """One pipeline call; returns ``{label: score}``."""
        clf = self._get_pipeline()
        result = clf(
            state,
            candidate_labels=labels,
            hypothesis_template=template,
            multi_label=multi_label,
        )
        self.calls += 1
        returned = result.get("labels", [])
        scores = result.get("scores", [])
        return {
            str(label): float(score)
            for label, score in zip(returned, scores)
            if label is not None
        }

    # ── evaluation ───────────────────────────────────────────

    def evaluate(
        self, state: str, questions: Mapping[str, Question]
    ) -> DecisionResponse:
        started = time.perf_counter()
        answers: Dict[str, Answer] = {}
        for qid, question in questions.items():
            try:
                answer = self._answer_question(state, question)
            except EncoderUnavailable:
                raise
            except Exception as exc:  # one bad question must not lose the rest
                logger.warning("encoder question %s failed: %s", qid, exc)
                continue
            if answer is not None:
                answers[qid] = answer
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        return DecisionResponse(
            provider=self.name,
            model=self.model,
            answers=answers,
            usage={},
            latency_ms=self.last_latency_ms,
        )

    def _answer_question(self, state: str, question: Question) -> Optional[Answer]:
        if isinstance(question, Choice):
            labels, by_label = self._option_labels(question)
            if not labels:
                return None
            scores = self._run(state, labels, self.choice_template, multi_label=False)
            if not scores:
                return None
            probabilities = normalize_probabilities(
                {key: scores.get(label, 0.0) for label, key in by_label.items()},
                question.option_keys(),
            )
            choice = max(probabilities, key=lambda key: probabilities[key])
            return ChoiceAnswer(
                choice=choice,
                probabilities=probabilities,
                confidence=confidence_from_probabilities(probabilities.values()),
            )

        if isinstance(question, Noul):
            statement = question.statement or question.instructions
            scores = self._run(
                state, [statement], self.statement_template, multi_label=True
            )
            value = scores.get(statement)
            if value is None:
                return None
            return NoulAnswer(noul=max(0.0, min(1.0, value)))

        # Score: every level judged on its own, then normalized across levels.
        scores = self._run(
            state, list(question.levels), self.choice_template, multi_label=True
        )
        if not scores:
            return None
        keys = question.level_keys()
        probabilities = normalize_probabilities(
            {key: scores.get(label, 0.0) for key, label in zip(keys, question.levels)},
            keys,
        )
        weighted = sum(int(key) * prob for key, prob in probabilities.items())
        return ScoreAnswer(
            score=weighted,
            legend={str(idx): label for idx, label in enumerate(question.levels)},
            probabilities=probabilities,
            confidence=confidence_from_probabilities(probabilities.values()),
        )

    @staticmethod
    def _option_labels(question: Choice) -> tuple[list[str], Dict[str, str]]:
        """Candidate labels for a Choice, plus the label -> option id map.

        Descriptions are used when present because an entailment hypothesis
        needs a statement, not an id. Labels stay unique even when two options
        share a description (the key is prefixed), so the returned order can
        always be mapped back to exactly one option.
        """
        labels: list[str] = []
        by_label: Dict[str, str] = {}
        for key, description in question.options.items():
            label = description.strip() if description and description.strip() else key
            if label in by_label:
                label = f"{key}: {label}"
            by_label[label] = key
            labels.append(label)
        return labels, by_label
