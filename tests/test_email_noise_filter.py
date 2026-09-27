# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""System mail is skipped before the detector reads it.

A five-message cycle over a real mailbox took roughly twenty minutes because
four of the messages were Microsoft security digests: a local model read each
one in full, and detection then discarded all four. These tests pin the filter
that runs first, and pin what it must not touch.
"""

import pytest

from threadweave.connectors.email.processor import EmailProcessor
from threadweave.connectors.email.watcher import EmailMessage, EmailThread
from threadweave.detector import ContentType, DetectionResult


def message(sender="Lars Hansen <lars@example.com>", subject="Rollback runbook",
            body=None):
    return EmailMessage(
        message_id="m1",
        conversation_id="c1",
        subject=subject,
        sender_name="",
        sender_email=sender,
        recipients=["owner@example.com"],
        body_text=body or (
            "We have decided to standardise the rollback runbook for the "
            "platform, this decision is finalized and documented for the team."
        ),
        received_at="2026-09-27T13:31:38Z",
        web_link="https://outlook.office365.com/owa/?ItemID=x",
    )


def detection(should_save=False, confidence=0.1):
    return DetectionResult(
        content_type=ContentType.DECISION,
        confidence=confidence,
        suggested_scope="team",
    )


@pytest.mark.parametrize("sender", [
    "MSSecurity-noreply@microsoft.com",
    "no-reply@vendor.example",
    "donotreply@example.com",
    "mailer-daemon@example.com",
    "notifications@github.com",
])
def test_automated_senders_are_skipped(sender):
    proc = EmailProcessor()
    assert proc._is_system_noise(message(sender=sender))
    assert proc.stats["noise_skipped"] == 0  # the check is side-effect free


@pytest.mark.parametrize("subject", [
    "Your weekly PIM digest for acme (ID: 11111111)",
    "Microsoft Entra ID Protection Weekly Digest",
    "Monthly report for the platform team",
    "The Platform Newsletter",
])
def test_recurring_digest_subjects_are_skipped(subject):
    assert EmailProcessor()._is_system_noise(message(subject=subject))


@pytest.mark.parametrize("subject", [
    "Digest of the client meeting",     # a person, writing about a digest
    "Weekly plan for the rollout",      # cadence word without a digest noun
    "Monthly summary for the platform team",  # a person's summary is knowledge
    "Re: Your weekly PIM digest for acme (ID: 1)",   # a person answered it
    "Fwd: Microsoft Entra ID Protection Weekly Digest",  # a person passed it on
    "Decision on the vendor onboarding checklist",
])
def test_a_person_writing_about_a_digest_is_not_skipped(subject):
    assert EmailProcessor()._is_system_noise(message(subject=subject)) == ""


@pytest.mark.asyncio
async def test_a_skipped_digest_never_reaches_the_detector(monkeypatch):
    from threadweave.connectors.email import processor as mod

    calls = []

    async def detector(*args, **kwargs):
        calls.append(args)
        raise AssertionError("detection must not run for system mail")

    monkeypatch.setattr(mod, "is_worth_saving_async", detector)
    proc = EmailProcessor()
    result = await proc.process_message(
        message(sender="MSSecurity-noreply@microsoft.com",
                subject="Microsoft Entra ID Protection Weekly Digest")
    )

    assert calls == []
    assert result.should_save is False
    assert proc.stats["noise_skipped"] == 1
    assert proc.stats["skipped"] == 1


@pytest.mark.asyncio
async def test_a_person_decision_still_reaches_the_detector(monkeypatch):
    from threadweave.connectors.email import processor as mod

    calls = []

    async def detector(text, *args, **kwargs):
        calls.append(text)
        return False, detection()

    monkeypatch.setattr(mod, "is_worth_saving_async", detector)
    proc = EmailProcessor()
    result = await proc.process_message(message())

    assert calls, "a person's mail must still be judged on its content"
    assert result.should_save is False
    assert proc.stats["noise_skipped"] == 0


def test_the_filter_can_be_switched_off(monkeypatch):
    """An operator who disagrees with the rule set must be able to say so."""
    monkeypatch.setenv("THREADWEAVE_EMAIL_NOISE_FILTER", "0")
    proc = EmailProcessor()
    assert proc.noise_filter_enabled is False
    assert proc._is_system_noise(
        message(sender="MSSecurity-noreply@microsoft.com",
                subject="Microsoft Entra ID Protection Weekly Digest")
    ) == ""


@pytest.mark.asyncio
async def test_a_thread_of_digests_is_skipped_but_a_mixed_thread_is_not(monkeypatch):
    from threadweave.connectors.email import processor as mod

    async def detector(*args, **kwargs):
        return False, detection()

    monkeypatch.setattr(mod, "is_worth_saving_async", detector)
    proc = EmailProcessor()
    digest = message(sender="MSSecurity-noreply@microsoft.com",
                     subject="Your weekly PIM digest for acme (ID: 1)")
    human = message(subject="Re: Your weekly PIM digest for acme (ID: 1)")

    await proc.process_thread(EmailThread(
        conversation_id="c1", subject=digest.subject, messages=[digest]))
    assert proc.stats["noise_skipped"] == 1

    await proc.process_thread(EmailThread(
        conversation_id="c1", subject=human.subject, messages=[digest, human]))
    assert proc.stats["noise_skipped"] == 1, "a human reply keeps the thread"


def test_the_cycle_line_reports_filtered_system_mail():
    """'skipped' alone cannot tell an operator an empty mailbox from a filtered one."""
    from threadweave.connectors.email.daemon import EmailWatchDaemon

    daemon = EmailWatchDaemon(watcher=None, processor=None, mailbox="o@example.com")
    result = {"fetched": 5, "processed": 5, "submitted": 1, "skipped": 4,
              "noise_skipped": 0, "errors": 0}

    from threadweave.connectors.email.processor import ProcessedEmail

    daemon._tally(ProcessedEmail(
        source="single", conversation_id="c1", subject="Weekly digest",
        participants=[], text_content="", word_count=0,
        skipped_reason="automated sender mssecurity-noreply@microsoft.com",
        noise=True,
    ), result)

    assert result["noise_skipped"] == 1
    assert daemon.stats["noise_skipped"] == 1
    line = daemon._summarize(result)
    assert "noise=1" in line and "errors=0" in line
    assert daemon._summarize({"fetched": 0, "processed": 0, "submitted": 0,
                              "skipped": 0, "errors": 0}).endswith("errors=0")
