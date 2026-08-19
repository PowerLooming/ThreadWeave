# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for tunable email thresholds (env-configurable save threshold + body floor)."""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.email.processor import EmailProcessor


def test_min_confidence_env_override(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_EMAIL_MIN_CONFIDENCE", "0.65")
    proc = EmailProcessor()
    assert proc.min_confidence == 0.65


def test_min_body_length_env_override(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_EMAIL_MIN_BODY_LENGTH", "250")
    proc = EmailProcessor()
    assert proc.min_body_length == 250


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_EMAIL_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("THREADWEAVE_EMAIL_MIN_BODY_LENGTH", raising=False)
    proc = EmailProcessor()
    assert proc.min_confidence == 0.40
    assert proc.min_body_length == 100


def test_explicit_args_win_over_env(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_EMAIL_MIN_CONFIDENCE", "0.65")
    proc = EmailProcessor(min_confidence=0.30, min_body_length=80)
    assert proc.min_confidence == 0.30
    assert proc.min_body_length == 80


@pytest.mark.asyncio
async def test_async_save_threshold_is_threaded(monkeypatch):
    # The email path now calls is_worth_saving_async (LLM if configured,
    # regex fallback). Prove the tuned threshold flows through the fallback
    # path end to end.
    monkeypatch.delenv("THREADWEAVE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("THREADWEAVE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from threadweave.llm_detector import reset_llm_detector
    reset_llm_detector()

    from threadweave.detector import is_worth_saving_async
    text = "We decided to use Postgres for the new service."
    should_low, _ = await is_worth_saving_async(text, threshold=0.0)
    should_high, _ = await is_worth_saving_async(text, threshold=0.99)
    assert should_low is True
    assert should_high is False
