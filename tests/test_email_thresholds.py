# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for tunable email thresholds (env-configurable save threshold + body floor)."""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.email.processor import EmailProcessor


def make_email(subject="s", body_text="", body_html="", mid="m1", conv="c1",
               sender="a@x.com", recipients=("b@x.com",)):
    return SimpleNamespace(
        message_id=mid, conversation_id=conv, subject=subject,
        body_text=body_text, body_html=body_html,
        sender_email=sender, recipients=list(recipients), cc_recipients=[],
    )


# ── NDR / bounce skipping ────────────────────────────────────

def test_ndr_subject_is_bounce():
    proc = EmailProcessor()
    assert proc._is_bounce(make_email(subject="Undeliverable: ThreadWeave captured your Team"))
    assert proc._is_bounce(make_email(subject="Delivery Status Notification (Failure)"))
    assert proc._is_bounce(make_email(subject="Mail Delivery Failed: your message"))


def test_ndr_body_marker_is_bounce():
    proc = EmailProcessor()
    body = (
        "Your message wasn't delivered.\n"
        "Diagnostic-Code: smtp;550 5.1.1 User unknown\n"
        "X-MS-Exchange-Antispam: BCL:0;ARA:13230040|23010399003\n"
    )
    assert proc._is_bounce(make_email(subject="Delivery failure", body_text=body))


def test_normal_email_is_not_bounce():
    proc = EmailProcessor()
    assert not proc._is_bounce(make_email(
        subject="Re: Postgres decision",
        body_text="We decided to use PostgreSQL for the auth service.",
    ))


@pytest.mark.asyncio
async def test_process_message_skips_bounce(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("THREADWEAVE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from threadweave.llm_detector import reset_llm_detector
    reset_llm_detector()

    proc = EmailProcessor()
    out = await proc.process_message(make_email(
        subject="Undeliverable: ThreadWeave captured your Team",
        body_text="Your message wasn't delivered. Diagnostic-Code: smtp;550",
    ))
    assert out.should_save is False
    assert out.text_content == ""
    assert proc.stats["skipped"] == 1


@pytest.mark.asyncio
async def test_process_thread_skips_bounce_when_any_message_bounced(monkeypatch):
    monkeypatch.delenv("THREADWEAVE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("THREADWEAVE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from threadweave.llm_detector import reset_llm_detector
    reset_llm_detector()

    proc = EmailProcessor()
    thread = SimpleNamespace(
        conversation_id="c1", subject="ThreadWeave captured your Team",
        messages=[
            make_email(subject="ThreadWeave captured your Team",
                       body_text="We decided to move the pilot to weekly."),
            make_email(subject="Undeliverable: ThreadWeave captured your Team",
                       body_text="Your message wasn't delivered."),
        ],
    )
    out = await proc.process_thread(thread)
    assert out.should_save is False
    assert out.text_content == ""
    assert proc.stats["skipped"] == 1


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
