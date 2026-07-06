"""
Tests for the detection engine.
"""

import pytest
from threadweave.detector import detect, is_worth_saving, ContentType


class TestDetectionEngine:

    # ── ANSWER detection ──────────────────────────────────

    def test_answer_explanation(self):
        text = (
            "The reason we use Postgres over MySQL is because we need "
            "full-text search and JSONB support. We evaluated both in 2022 "
            "and Postgres came out ahead on every benchmark."
        )
        result = detect(text)
        assert result.content_type == ContentType.ANSWER
        assert result.confidence >= 0.2

    def test_answer_instruction(self):
        text = (
            "You need to run the migration script BEFORE deploying the new "
            "version. Always check the CI pipeline first — if it's red, "
            "the deploy will fail. Never skip this step."
        )
        result = detect(text)
        assert result.content_type == ContentType.ANSWER
        assert result.confidence > 0.2

    def test_answer_pattern(self):
        text = (
            "In my experience, the pattern here is always the same: "
            "the cache warms up slowly after a deploy. Note that you should "
            "wait at least 5 minutes before running load tests."
        )
        result = detect(text)
        assert result.content_type == ContentType.ANSWER

    # ── DECISION detection ────────────────────────────────

    def test_decision_explicit(self):
        text = (
            "Decision: We will use GraphQL for the new API. "
            "We considered REST and gRPC. GraphQL was chosen because "
            "the frontend team needs flexible queries and we have "
            "three strategic customers requiring it."
        )
        result = detect(text)
        assert result.content_type == ContentType.DECISION
        assert result.confidence > 0.2

    def test_decision_approved(self):
        text = (
            "Architecture review approved. We're going with the event-driven "
            "approach. The team decided this was the right call given our "
            "scale requirements."
        )
        result = detect(text)
        assert result.content_type == ContentType.DECISION

    # ── QUESTION detection ────────────────────────────────

    def test_question(self):
        text = "How do we handle authentication for the new service?"
        result = detect(text)
        assert result.content_type == ContentType.QUESTION

    def test_question_anyone(self):
        text = "Does anyone know why the billing service keeps timing out?"
        result = detect(text)
        assert result.content_type == ContentType.QUESTION

    # ── CHAT detection ────────────────────────────────────

    def test_chat_too_short(self):
        text = "ok thanks"
        result = detect(text)
        assert result.content_type == ContentType.CHAT

    def test_chat_casual(self):
        text = "Hey, are you free for lunch later? I was thinking about trying that new place on 5th."
        result = detect(text)
        assert result.content_type == ContentType.CHAT

    # ── Entity extraction ─────────────────────────────────

    def test_extract_technologies(self):
        text = (
            "We use Docker for deployment because it simplifies our pipeline, "
            "Postgres for storage since we need JSONB, "
            "and Redis for caching. The API is built with Python FastAPI."
        )
        result = detect(text)
        techs = [e["value"] for e in result.entities if e["type"] == "technology"]
        assert "Docker" in techs
        assert "Postgres" in techs
        assert "Redis" in techs

    # ── is_worth_saving ───────────────────────────────────

    def test_worth_saving_answer(self):
        text = (
            "The reason we always restart the service after a config change "
            "is that the config loader caches on startup. There's no hot reload."
        )
        should, result = is_worth_saving(text)
        assert should is True
        assert result.content_type == ContentType.ANSWER

    def test_not_worth_saving_chat(self):
        text = "Sounds good, let me know when it's ready."
        should, result = is_worth_saving(text)
        assert should is False

    # ── PII detection ─────────────────────────────────────

    def test_pii_detection_email(self):
        text = "You can reach out to john.doe@company.com for access to the dashboard."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_detection_phone(self):
        text = "Call me at 555-123-4567 if you need help with the deploy."
        result = detect(text)
        assert result.has_pii is True

    # ── Scope suggestion ──────────────────────────────────

    def test_scope_department(self):
        text = (
            "This is a department-wide policy. Everyone in engineering "
            "should follow this pattern for all new services."
        )
        result = detect(text)
        assert "department" in result.suggested_scope or "organization" in result.suggested_scope

    # ── Long-form confidence boost ────────────────────────

    def test_long_form_boost(self):
        short = "We use Postgres because it's better."
        long = (
            "We use Postgres because it's better. " * 20
        )
        result_short = detect(short)
        result_long = detect(long)
        assert result_long.confidence > result_short.confidence
