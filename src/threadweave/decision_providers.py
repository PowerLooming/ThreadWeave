# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Decision providers — who actually answers the typed questions.

Two backends behind one interface (:class:`~threadweave.decisions.DecisionProvider`):

* ``OllamaDecisionProvider`` — local, on-prem, no content leaves the machine.
  One batched call to ``/api/chat`` with a JSON schema pinned as the response
  format, so the model must answer in the shape the policy expects. All the
  questions for one message ride in a single request; that batching is the
  whole point of the layer. Its probabilities are self-reported by a generative
  model, which makes its confidences uncalibrated by construction.
* ``LayaDecisionProvider`` (``decision_laya.py``) — a local non-autoregressive
  decision model, one forward pass for all questions (optional, opt-in)
* ``EncoderDecisionProvider`` (``decision_encoder.py``) — a small local NLI
  classifier that returns real distributions computed from logits. One call per
  question, because the softmax semantics only hold inside a question.
* ``TypeSafeDecisionProvider`` — the seam for a hosted "System One" decision
  model (Jev, https://docs.typesafe.ai). It is implemented against the
  documented ``POST /v1/systemone`` contract and is OFF unless remote
  content is explicitly allowed, because ThreadWeave's contract is that
  message content stays on-premise. See ``_require_remote_allowed``.

Providers are synchronous by design (easy to fake in tests, no event-loop
juggling); the gate calls them from a worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Mapping, Optional

import httpx

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
    env_flag,
    normalize_probabilities,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OllamaDecisionProvider",
    "TypeSafeDecisionProvider",
    "RemoteContentNotAllowed",
    "get_decision_provider",
    "reset_decision_provider",
]

TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"


class RemoteContentNotAllowed(RuntimeError):
    """A remote provider was asked to evaluate content without opt-in."""


def _extract_json_object(content: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Local models still wrap JSON in fences or prose even when the response
    format is pinned, so this stays tolerant: direct parse, then fenced
    block, then the outermost braces.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("empty model response")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    braced = re.search(r"\{.*\}", text, re.DOTALL)
    if braced:
        try:
            parsed = json.loads(braced.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise ValueError(f"no JSON object in model response: {text[:200]}")


def _as_probability(value: Any) -> float:
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return 0.0
    if prob != prob:  # NaN
        return 0.0
    return max(0.0, min(1.0, prob))


# ── Ollama (local, on-prem) ──────────────────────────────────


def _question_schema(questions: Mapping[str, Question]) -> Dict[str, Any]:
    """JSON schema every question's answer must satisfy (Ollama ``format``)."""
    properties: Dict[str, Any] = {}
    for qid, question in questions.items():
        if isinstance(question, Noul):
            properties[qid] = {
                "type": "object",
                "properties": {"noul": {"type": "number"}},
                "required": ["noul"],
            }
        elif isinstance(question, Choice):
            options = question.option_keys()
            properties[qid] = {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": options},
                    "probabilities": {
                        "type": "object",
                        "properties": {opt: {"type": "number"} for opt in options},
                        "required": options,
                    },
                },
                "required": ["choice", "probabilities"],
            }
        else:  # Score
            levels = question.level_keys()
            properties[qid] = {
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                    "probabilities": {
                        "type": "object",
                        "properties": {lvl: {"type": "number"} for lvl in levels},
                        "required": levels,
                    },
                },
                "required": ["score", "probabilities"],
            }
    return {
        "type": "object",
        "properties": properties,
        "required": list(questions.keys()),
    }


def _question_brief(questions: Mapping[str, Question]) -> str:
    """Readable listing of the questions for the prompt body."""
    lines: list[str] = []
    for qid, question in questions.items():
        if isinstance(question, Noul):
            lines.append(f'- "{qid}" (noul): {question.instructions} -> {{"noul": <0..1>}}')
            crit = question.criteria()
            if crit:
                lines.append(
                    "    true means: {}; false means: {}".format(
                        crit.get("true", "yes"), crit.get("false", "no")
                    )
                )
        elif isinstance(question, Choice):
            options = "; ".join(
                f"{key} = {desc}" if desc else key for key, desc in question.options.items()
            )
            lines.append(
                f'- "{qid}" (choice): {question.instructions}\n'
                f"    options: {options}\n"
                f'    -> {{"choice": <one option id>, "probabilities": {{<option id>: <0..1>, ...}}}}'
            )
        else:  # Score
            levels = "; ".join(
                f"{idx} = {label}" for idx, label in enumerate(question.levels)
            )
            lines.append(
                f'- "{qid}" (score): {question.instructions}\n'
                f"    levels: {levels}\n"
                f'    -> {{"score": <level number>, "probabilities": {{"0": <0..1>, ...}}}}'
            )
    return "\n".join(lines)


OLLAMA_SYSTEM_PROMPT = (
    "You are a decision engine for an organizational memory system. You do not "
    "write prose and you do not explain. For each question you return one typed "
    "answer about the message you are given.\n\n"
    "Answer rules:\n"
    "1. Return ONE JSON object whose keys are exactly the question ids below. "
    "No markdown, no commentary, no extra keys.\n"
    "2. A noul answer is the probability (0.0-1.0) that the statement is true. "
    "Use values near 0 or 1 only when you are actually sure; a genuine coin flip "
    "is 0.5 and that is a useful answer.\n"
    "3. A choice answer must be one of the listed option ids, and its "
    "probabilities must cover every option and sum to 1.0.\n"
    "4. A score answer is the level number you rate the message at, with "
    "probabilities covering every level and summing to 1.0. It may sit between "
    "levels (e.g. 1.5).\n"
    "5. Judge only what the message itself says. Ticket numbers, URLs and file "
    "names do not make content administrative: judge the surrounding knowledge.\n\n"
    "Questions:\n{questions}"
)


class OllamaDecisionProvider(DecisionProvider):
    """Local batched decision provider speaking Ollama's native API."""

    name = "ollama"

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.timeout = timeout
        self._client = client
        self.requests = 0
        self.last_latency_ms = 0.0

    # ── plumbing ─────────────────────────────────────────────

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}/api/chat"

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.timeout))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_available(self) -> bool:
        return bool(self.base_url and self.model)

    # ── evaluation ───────────────────────────────────────────

    def evaluate(
        self, state: str, questions: Mapping[str, Question]
    ) -> DecisionResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": OLLAMA_SYSTEM_PROMPT.format(
                        questions=_question_brief(questions)
                    ),
                },
                {"role": "user", "content": f"Message to judge:\n\n{state}"},
            ],
            "stream": False,
            "think": False,
            "format": _question_schema(questions),
            "options": {"temperature": 0.0, "num_predict": 400},
        }

        started = time.perf_counter()
        client = self._get_client()
        endpoint = self._endpoint()
        response = client.post(endpoint, json=payload)
        if response.status_code == 400 and isinstance(payload.get("format"), dict):
            # Older Ollama builds reject a JSON-schema `format`; plain JSON
            # mode plus the prompt still pins the shape.
            logger.debug("ollama rejected schema format, retrying with format=json")
            payload["format"] = "json"
            response = client.post(endpoint, json=payload)
        response.raise_for_status()
        data = response.json()
        self.requests += 1
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0

        content = (data.get("message") or {}).get("content", "")
        parsed = _extract_json_object(content)
        answers = self._build_answers(parsed, questions)

        return DecisionResponse(
            provider=self.name,
            model=data.get("model") or self.model,
            answers=answers,
            usage={
                "input_tokens": int(data.get("prompt_eval_count") or 0),
                "output_tokens": int(data.get("eval_count") or 0),
            },
            latency_ms=self.last_latency_ms,
        )

    @staticmethod
    def _build_answers(
        parsed: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Dict[str, Answer]:
        """Coerce raw model output into typed answers; skip unanswered ids."""
        answers: Dict[str, Answer] = {}
        for qid, question in questions.items():
            raw = parsed.get(qid)
            if not isinstance(raw, Mapping):
                continue
            if isinstance(question, Noul):
                if "noul" not in raw:
                    continue
                answers[qid] = NoulAnswer(noul=_as_probability(raw.get("noul")))
                continue
            if isinstance(question, Choice):
                options = question.option_keys()
                chosen = str(raw.get("choice", "")).strip()
                if chosen not in options:
                    lowered = {opt.lower(): opt for opt in options}
                    chosen = lowered.get(chosen.lower(), "")
                if not chosen:
                    continue
                probs_raw = raw.get("probabilities")
                probs = (
                    normalize_probabilities(probs_raw, options)
                    if isinstance(probs_raw, Mapping)
                    else {opt: 1.0 if opt == chosen else 0.0 for opt in options}
                )
                answers[qid] = ChoiceAnswer(
                    choice=chosen,
                    probabilities=probs,
                    confidence=confidence_from_probabilities(probs.values()),
                )
                continue
            # Score
            levels = question.level_keys()
            try:
                score = float(raw.get("score"))
            except (TypeError, ValueError):
                continue
            probs_raw = raw.get("probabilities")
            probs = (
                normalize_probabilities(probs_raw, levels)
                if isinstance(probs_raw, Mapping)
                else normalize_probabilities({str(int(round(score))): 1.0}, levels)
            )
            answers[qid] = ScoreAnswer(
                score=score,
                legend={idx: label for idx, label in enumerate(question.levels)},
                probabilities=probs,
                confidence=confidence_from_probabilities(probs.values()),
            )
        return answers


# ── TypeSafe (hosted seam, off by default) ───────────────────


class TypeSafeDecisionProvider(DecisionProvider):
    """Hosted System One decision model (Jev).

    Implemented against the documented API: ``POST /v1/systemone`` with
    ``{state, model, questions}`` and answers keyed by question id. It is
    never used for content unless ``THREADWEAVE_DECISION_ALLOW_REMOTE`` is
    truthy, because ThreadWeave's privacy contract is one-way and on-prem:
    content does not leave the machine it was captured on.
    """

    name = "typesafe"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "jev-latest",
        allow_remote: Optional[bool] = None,
        timeout: float = 20.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.allow_remote = (
            env_flag("THREADWEAVE_DECISION_ALLOW_REMOTE") if allow_remote is None
            else allow_remote
        )
        self.timeout = timeout
        self._client = client
        self.requests = 0

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_available(self) -> bool:
        return bool(self.api_key and self.allow_remote)

    def _require_remote_allowed(self) -> None:
        if not self.allow_remote:
            raise RemoteContentNotAllowed(
                "TypeSafe is a hosted API and ThreadWeave content stays on-prem. "
                "Set THREADWEAVE_DECISION_ALLOW_REMOTE=1 only for states that carry "
                "no message content, or keep the local provider."
            )

    def evaluate(
        self, state: str, questions: Mapping[str, Question]
    ) -> DecisionResponse:
        self._require_remote_allowed()
        if not self.api_key:
            raise RuntimeError("THREADWEAVE_DECISION_API_KEY is not set")

        payload = {
            "state": state,
            "model": self.model,
            "questions": {qid: q.to_wire() for qid, q in questions.items()},
        }
        started = time.perf_counter()
        client = self._get_client()
        response = client.post(TYPESAFE_ENDPOINT, json=payload)
        if response.status_code in (429, 529):
            time.sleep(1.0)
            response = client.post(TYPESAFE_ENDPOINT, json=payload)
        response.raise_for_status()
        data = response.json()
        self.requests += 1

        return DecisionResponse(
            provider=self.name,
            model=data.get("model") or self.model,
            answers=self._build_answers(data.get("answers") or {}, questions),
            usage={
                "input_tokens": int((data.get("usage") or {}).get("input_tokens") or 0),
                "output_tokens": int((data.get("usage") or {}).get("output_tokens") or 0),
            },
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    @staticmethod
    def _build_answers(
        parsed: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Dict[str, Answer]:
        answers: Dict[str, Answer] = {}
        for qid, question in questions.items():
            raw = parsed.get(qid)
            if not isinstance(raw, Mapping):
                continue
            if isinstance(question, Noul):
                if raw.get("noul") is None:
                    continue
                answers[qid] = NoulAnswer(noul=_as_probability(raw.get("noul")))
                continue
            if isinstance(question, Choice):
                options = question.option_keys()
                chosen = str(raw.get("choice", "")).strip()
                if chosen not in options:
                    continue
                probs_raw = raw.get("probabilities")
                probs = (
                    normalize_probabilities(probs_raw, options)
                    if isinstance(probs_raw, Mapping)
                    else {opt: 1.0 if opt == chosen else 0.0 for opt in options}
                )
                confidence = raw.get("confidence")
                answers[qid] = ChoiceAnswer(
                    choice=chosen,
                    probabilities=probs,
                    confidence=(
                        _as_probability(confidence)
                        if confidence is not None
                        else confidence_from_probabilities(probs.values())
                    ),
                )
                continue
            levels = question.level_keys()
            try:
                score = float(raw.get("score"))
            except (TypeError, ValueError):
                continue
            probs_raw = raw.get("probabilities")
            probs = (
                normalize_probabilities(probs_raw, levels)
                if isinstance(probs_raw, Mapping)
                else normalize_probabilities({str(int(round(score))): 1.0}, levels)
            )
            confidence = raw.get("confidence")
            answers[qid] = ScoreAnswer(
                score=score,
                legend={idx: label for idx, label in enumerate(question.levels)},
                probabilities=probs,
                confidence=(
                    _as_probability(confidence)
                    if confidence is not None
                    else confidence_from_probabilities(probs.values())
                ),
            )
        return answers


# ── Factory ──────────────────────────────────────────────────

_provider: Optional[DecisionProvider] = None


def get_decision_provider() -> Optional[DecisionProvider]:
    """Build the configured provider, or None when the layer is disabled.

    ``THREADWEAVE_DECISION_PROVIDER`` selects the backend:
    ``encoder`` (local NLI classifier, real probability distributions),
    ``laya`` (local non-autoregressive decision model, one pass per message),
    ``ollama`` (local generative model, self-reported probabilities),
    ``typesafe`` (hosted, needs the remote opt-in), or empty/``off``/``none``
    which leaves the typed layer switched off and the existing detector path
    untouched.
    """
    global _provider
    if _provider is not None:
        return _provider

    choice = (os.environ.get("THREADWEAVE_DECISION_PROVIDER") or "").strip().lower()
    if choice in ("", "off", "none", "false", "0"):
        return None

    if choice == "encoder":
        from threadweave.decision_encoder import (
            DEFAULT_ENCODER_MODEL,
            EncoderDecisionProvider,
        )

        _provider = EncoderDecisionProvider(
            model=os.environ.get("THREADWEAVE_DECISION_MODEL") or DEFAULT_ENCODER_MODEL,
        )
        return _provider

    if choice == "laya":
        from threadweave.decision_laya import (
            CHECKPOINT_ENGLISH,
            CHECKPOINT_MULTILINGUAL,
            LayaDecisionProvider,
        )

        _provider = LayaDecisionProvider(
            model=os.environ.get("THREADWEAVE_DECISION_LAYA_MODEL") or "",
            checkpoint_en=(
                os.environ.get("THREADWEAVE_DECISION_LAYA_CHECKPOINT_EN")
                or CHECKPOINT_ENGLISH
            ),
            checkpoint_other=(
                os.environ.get("THREADWEAVE_DECISION_LAYA_CHECKPOINT_OTHER")
                or CHECKPOINT_MULTILINGUAL
            ),
            preload=(os.environ.get("THREADWEAVE_DECISION_LAYA_PRELOAD") or "")
            .strip()
            .lower()
            in ("1", "true", "yes"),
            noul_form=os.environ.get("THREADWEAVE_DECISION_LAYA_NOUL_FORM") or "choice",
        )
        return _provider

    if choice == "ollama":
        _provider = OllamaDecisionProvider(
            base_url=os.environ.get("THREADWEAVE_LLM_BASE_URL") or DEFAULT_OLLAMA_URL,
            model=(
                os.environ.get("THREADWEAVE_DECISION_MODEL")
                or os.environ.get("THREADWEAVE_LLM_MODEL")
                or DEFAULT_OLLAMA_MODEL
            ),
            timeout=float(os.environ.get("THREADWEAVE_DECISION_TIMEOUT", "60")),
        )
        return _provider

    if choice == "typesafe":
        _provider = TypeSafeDecisionProvider(
            api_key=(
                os.environ.get("THREADWEAVE_DECISION_API_KEY")
                or os.environ.get("TYPESAFE_API_KEY")
                or ""
            ),
            model=os.environ.get("THREADWEAVE_DECISION_MODEL") or "jev-latest",
            timeout=float(os.environ.get("THREADWEAVE_DECISION_TIMEOUT", "20")),
        )
        return _provider

    logger.warning("unknown THREADWEAVE_DECISION_PROVIDER=%r — layer disabled", choice)
    return None


def reset_decision_provider() -> None:
    """Drop the cached provider (tests, config changes)."""
    global _provider
    if _provider is not None:
        try:
            _provider.close()
        except Exception:  # closing a dead transport must not raise
            logger.debug("provider close failed", exc_info=True)
    _provider = None
