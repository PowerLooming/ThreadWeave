# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
LLM-based detection engine for ThreadWeave.

Uses an OpenAI-compatible chat/completions endpoint to classify workplace
communication with higher accuracy than regex heuristics. Designed to handle
the nuance that regex misses: domain-specific reasoning, implicit decisions
("after evaluating the regulatory exposure, we recommend model B"), and
context-dependent PII judgments.

Gracefully falls back to regex when no API key is configured or the LLM
call fails. No new mandatory dependencies — uses httpx which is already
in the project.

Configuration (environment variables):
    THREADWEAVE_LLM_API_KEY    API key (falls back to OPENAI_API_KEY)
    THREADWEAVE_LLM_BASE_URL   Base URL (falls back to OPENAI_BASE_URL)
    THREADWEAVE_LLM_MODEL      Model name (default: gpt-4o-mini)
    THREADWEAVE_LLM_PROVIDER   Provider hint (default: openai)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from threadweave.detector import (
    ContentType,
    DetectionResult,
    detect as regex_detect,
    is_worth_saving as regex_is_worth_saving,
)


# ── Prompt ────────────────────────────────────────────────────
# Kept as a module-level string so it's easy to review, edit, and
# version-control without digging through code.

SYSTEM_PROMPT = """\
You are a content classifier for organizational memory. Your job is to analyze \
workplace communication and decide what should be preserved as long-term \
organizational knowledge.

Return ONLY a valid JSON object — no markdown fences, no extra text:

{
  "content_type": "answer" | "decision" | "question" | "chat" | "reference",
  "confidence": 0.0-1.0,
  "should_save": true | false,
  "entities": [{"type": "technology"|"person"|"organization"|"system","value":"name"}],
  "has_pii": true | false,
  "suggested_title": "short descriptive title (5-10 words, max 100 chars)",
  "suggested_scope": "team" | "department" | "organization",
  "reasoning": "one sentence explaining the classification (max 120 chars)"
}

CLASSIFICATION RULES:
* ANSWER — explanations, instructions, knowledge sharing, best practices,
  patterns, conventions, architecture explanations, technical deep-dives.
  WORTH SAVING.
* DECISION — explicit decisions with rationale, approvals/rejections,
  architecture decisions (ADR), technology/policy choices. WORTH SAVING.
* QUESTION — a question someone asked. NOT worth saving alone (save with answer).
* CHAT — casual chat, greetings, acknowledgments ("ok","thanks"), small-talk,
  status updates with no durable knowledge. NOT worth saving.
* REFERENCE — links, URLs, ticket numbers, document pointers.
  May be worth saving as metadata.

SCOPE:
* "team" — knowledge specific to one team's workflow, tools, conventions.
* "department" — relevant across multiple teams within a department.
* "organization" — company-wide policies, standards, or knowledge.

PII RULES (professional context):
* TRUE: email, phone, SSN, credit-card, passport, personal address.
* FALSE: generic roles ("the CEO said"), public professional names, company
  emails in a work context (alice@company.com is not PII in internal chat).

CONFIDENCE GUIDELINES:
* 0.9+  = crystal-clear signal, no ambiguity.
* 0.7-  = strong signal, minor ambiguity.
* 0.5-  = moderate, mixed indicators.
* 0.3-  = weak signal, best-guess.
* <0.3  = very uncertain, borderline chat.

ENTITIES: extract concrete technologies, systems, org names. Skip generic nouns.
TITLE: core topic in 5-10 words. For decisions prepend "Decision: "."""


# ── Config ─────────────────────────────────────────────────────


@dataclass
class LLMConfig:
    """LLM detector configuration — all from env vars with sensible defaults."""

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 500
    temperature: float = 0.0
    timeout: float = 60.0
    max_retries: int = 2
    min_content_length: int = 50  # shorter → regex (cheaper)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        api_key = (
            os.environ.get("THREADWEAVE_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        base_url = (
            os.environ.get("THREADWEAVE_LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        )
        model = (
            os.environ.get("THREADWEAVE_LLM_MODEL")
            or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        )
        provider = os.environ.get("THREADWEAVE_LLM_PROVIDER", "openai")
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )


# ── Detector ───────────────────────────────────────────────────


class LLMDetector:
    """LLM-powered content classifier.

    Uses any OpenAI-compatible chat/completions API (OpenAI, Azure OpenAI,
    Ollama, vLLM, Groq, Together, local models via llama.cpp server, etc.).

    Falls back to regex heuristics when:
    - No API key is configured
    - Content is too short (< min_content_length chars)
    - LLM call fails after retries

    Usage:
        detector = LLMDetector(LLMConfig.from_env())
        result = await detector.detect(text)          # async (preferred)
        result = detector.detect_sync(text)            # sync wrapper
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()
        self._client: Optional[httpx.AsyncClient] = None
        self._stats: dict[str, int | float] = {
            "calls": 0,
            "hits": 0,
            "misses": 0,
            "tokens_approx": 0,
        }

    # ── public API ──────────────────────────────────────────

    @property
    def available(self) -> bool:
        """LLM is configured (has API key and/or local base URL)."""
        return bool(self.config.api_key or self.config.base_url)

    async def detect(self, text: str, min_length: int = 50) -> DetectionResult:
        """Classify text. LLM if available and text long enough; regex otherwise."""
        if len(text) < min_length or len(text) < self.config.min_content_length:
            return regex_detect(text, min_length)

        if not self.available:
            return regex_detect(text, min_length)

        try:
            result = await self._classify_via_llm(text)
            self._stats["calls"] += 1
            self._stats["hits"] += 1
            return result
        except Exception:
            self._stats["calls"] += 1
            self._stats["misses"] += 1
            return regex_detect(text, min_length)

    def detect_sync(self, text: str, min_length: int = 50) -> DetectionResult:
        """Synchronous wrapper for contexts that can't use async (tests, repl)."""
        if not self.available or len(text) < min_length:
            return regex_detect(text, min_length)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Already inside an async event loop — can't nest.
            return regex_detect(text, min_length)
        return asyncio.run(self.detect(text, min_length))

    async def is_worth_saving(self, text: str) -> tuple[bool, DetectionResult]:
        """Async version of is_worth_saving."""
        result = await self.detect(text)
        should = (
            result.content_type in (ContentType.ANSWER, ContentType.DECISION)
            and result.confidence >= 0.15  # slightly lower threshold for LLM
        )
        return should, result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── internals ────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout),
                headers=headers,
            )
        return self._client

    def _resolve_url(self) -> str:
        """Resolve the chat/completions endpoint from config."""
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
        elif self.config.provider == "anthropic":
            base = "https://api.anthropic.com/v1"
        else:
            base = "https://api.openai.com/v1"
        return f"{base}/chat/completions"

    async def _classify_via_llm(self, text: str) -> DetectionResult:
        url = self._resolve_url()
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Classify this text:\n\n{text}"},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        # response_format is OpenAI-specific; Ollama/vLLM may not support it.
        # The prompt already mandates JSON-only output, so this is optional.
        if self.config.provider not in ("ollama",):
            payload["response_format"] = {"type": "json_object"}

        client = await self._get_client()

        last_error: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                # Rough token estimate from prompt + response
                prompt_tokens = data.get("usage", {}).get("prompt_tokens", 0)
                completion_tokens = data.get("usage", {}).get("completion_tokens", 0)
                self._stats["tokens_approx"] += prompt_tokens + completion_tokens
                parsed = self._parse_json_response(content)
                return self._to_detection_result(parsed)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))

        raise last_error or RuntimeError("LLM classification failed")

    @staticmethod
    def _parse_json_response(content: str) -> dict:
        """Extract JSON from LLM response (handles markdown wrapping)."""
        # 1. Direct parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # 2. Markdown code fence
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 3. Raw JSON object anywhere
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from LLM response: {content[:200]}")

    @staticmethod
    def _to_detection_result(parsed: dict) -> DetectionResult:
        """Convert parsed LLM JSON → DetectionResult, with validation."""
        type_map = {
            "answer": ContentType.ANSWER,
            "decision": ContentType.DECISION,
            "question": ContentType.QUESTION,
            "chat": ContentType.CHAT,
            "reference": ContentType.REFERENCE,
        }
        ct = type_map.get(str(parsed.get("content_type", "chat")).lower(), ContentType.CHAT)

        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        entities: list[dict] = []
        for e in parsed.get("entities", []):
            if isinstance(e, dict) and "type" in e and "value" in e:
                entities.append({"type": str(e["type"]), "value": str(e["value"])})

        scope = str(parsed.get("suggested_scope", "team"))
        if scope not in ("team", "department", "organization"):
            scope = "team"

        reasoning = str(parsed.get("reasoning", ""))[:120]

        return DetectionResult(
            content_type=ct,
            confidence=confidence,
            signals=[f"llm({confidence:.2f}): {reasoning}"] if reasoning else [],
            entities=entities,
            suggested_scope=scope,
            suggested_title=str(parsed.get("suggested_title", ""))[:100],
            has_pii=bool(parsed.get("has_pii", False)),
        )


# ── Module-level singleton ─────────────────────────────────────

_detector: Optional[LLMDetector] = None
_lock = asyncio.Lock()


def get_llm_detector() -> Optional[LLMDetector]:
    """Get or create the shared LLM detector (sync accessor)."""
    global _detector
    if _detector is None:
        config = LLMConfig.from_env()
        if config.api_key or config.base_url:
            _detector = LLMDetector(config)
    return _detector


def reset_llm_detector() -> None:
    """Reset singleton (for testing)."""
    global _detector
    _detector = None
