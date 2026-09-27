# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Human-nature battery for the gossip filter.

20+ cases spanning gossip, cruelty, backtalk, humor, venting, sarcasm,
and legitimate knowledge — the "what do humans actually write" test set
for the has_gossip flag (2026-08-19, designed from first principles by
imagining real office communication).

Layered design (verified live with qwen3.5:9b):
- Regex layer is CONSERVATIVE: high precision, zero false positives on
  legitimate content. It catches direct insults, "X is a Y" attacks,
  hearsay framing, relationship gossip, backtalk.
- LLM layer is the authority for semantics: "Anna is completely
  useless" (person vs thing), sarcasm targeting a person, mockery
  metaphors ("Ola is the human error message"). The LLM scored 15/15
  on the gossip cases and 10/10 on the legitimate cases in the live
  battery (2026-08-19).
"""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.detector import detect

# ── Regex-layer cases (deterministic, no LLM needed) ───────────

GOSSIP_REGEX_CASES = [
    # (category, text) — all must be flagged by the regex layer
    ("gossip",      "we have concluded that Patty is a bitch"),
    ("gossip",      "I heard that Bjorn is sleeping with the intern"),
    ("gossip",      "Apparently Kim got the promotion because she's dating the boss"),
    ("gossip",      "Word around the office is that Lars is about to be fired"),
    ("gossip",      "Did you hear what Maria did at the Christmas party? Total scandal"),
    ("cruelty",     "Jens is such an idiot, I can't believe he still has a job here"),
    ("cruelty",     "The new manager is a moron who only got the role because of nepotism"),
    ("backtalk",    "The boss can go screw himself with his new policy"),
]

LEGIT_REGEX_CASES = [
    # (category, text) — must NOT be flagged by the regex layer
    ("humor",       "Our server room is a sauna in summer, you could fry eggs on the rack"),
    ("humor",       "The coffee machine has better uptime than our build server, sad but true"),
    ("venting",     "This project is a nightmare, the requirements change every single week"),
    ("venting",     "I'm so fed up with this legacy codebase, nothing is documented anywhere"),
    ("sarcasm",     "Great, another meeting that could have been an email. Fantastic."),
    ("criticism",   "The PR has bugs and the design is weak, we should refactor it."),
    ("criticism",   "This API is completely useless, we should replace it"),
    ("decision",    "We decided to use PostgreSQL for the authentication service because of JSONB support"),
    ("answer",      "The root cause of the outage was a missing index; adding it cut latency by 90 percent"),
    ("review",      "Code review feedback: the auth refactor has a race condition, please fix before merge"),
]


class TestGossipRegexLayer:
    @pytest.mark.parametrize("cat,text", GOSSIP_REGEX_CASES)
    def test_gossip_flagged(self, cat, text):
        result = detect(text)
        assert result.has_gossip is True, (
            f"[{cat}] regex should flag gossip: {text!r} -> {result.has_gossip}"
        )

    @pytest.mark.parametrize("cat,text", LEGIT_REGEX_CASES)
    def test_legit_not_flagged(self, cat, text):
        result = detect(text)
        assert result.has_gossip is False, (
            f"[{cat}] regex must NOT flag legit content: {text!r} -> {result.has_gossip}"
        )


# ── LLM-layer cases (require Ollama; skipped when unavailable) ──

LLM_GOSSIP_CASES = [
    # Cases the regex is too conservative to catch — the LLM is the
    # authority. All verified live 2026-08-19 with qwen3.5:9b.
    "Anna is completely useless, she contributes nothing to this team",
    "I don't care what the leadership wants, they never listen to us anyway",
    "Ola is the human error message, everything crashes when he touches it lol",
    "Oh brilliant, Peter strikes again with another genius idea we all have to clean up",
    "Jeg hørte at Per er utro mot kona si",                 # Norwegian gossip
    "Kari er helt ubrukelig, hun får ingenting gjort",      # Norwegian cruelty
    "Sjefen kan dra til helvete med den nye politikken sin", # Norwegian backtalk
]

LLM_LEGIT_CASES = [
    "Vi bestemte oss for å bruke PostgreSQL for autentiseringstjenesten",
    "Dette prosjektet er et mareritt, kravene endrer seg hver uke",
    "We decided to use PostgreSQL for the authentication service",
]


def _llm_available() -> bool:
    """The base URL is what names an endpoint, so it is the whole test."""
    import os

    return bool(os.environ.get("THREADWEAVE_LLM_BASE_URL"))


@pytest.mark.skipif(not _llm_available(), reason="LLM detector not configured")
class TestGossipLLMLayer:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from threadweave.llm_detector import reset_llm_detector

        reset_llm_detector()
        yield
        reset_llm_detector()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", LLM_GOSSIP_CASES)
    async def test_llm_flags_gossip(self, text):
        from threadweave.llm_detector import LLMDetector

        d = LLMDetector()
        try:
            res = await d.detect(text)
            assert res.has_gossip is True, (
                f"LLM should flag gossip: {text!r} -> {res.has_gossip}"
            )
        finally:
            await d.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", LLM_LEGIT_CASES)
    async def test_llm_allows_legit(self, text):
        from threadweave.llm_detector import LLMDetector

        d = LLMDetector()
        try:
            res = await d.detect(text)
            assert res.has_gossip is False, (
                f"LLM must allow legit content: {text!r} -> {res.has_gossip}"
            )
        finally:
            await d.close()
