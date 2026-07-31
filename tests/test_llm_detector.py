# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Tests for LLM-based detection engine.

Covers:
- JSON parsing (direct, markdown-wrapped, malformed)
- DetectionResult conversion
- LLMDetector with mocked HTTP responses
- Graceful fallback to regex when LLM unavailable
- detect_async() auto-selection of regex vs LLM
- API ingest endpoint with LLM detection
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from threadweave.detector import (
    ContentType,
    DetectionResult,
    detect,
    detect_async,
    is_worth_saving_async,
)
from threadweave.llm_detector import (
    LLMConfig,
    LLMDetector,
    SYSTEM_PROMPT,
    get_llm_detector,
    reset_llm_detector,
)


# ── Helpers ────────────────────────────────────────────────────


def _make_llm_response(
    content_type: str = "answer",
    confidence: float = 0.85,
    should_save: bool = True,
    entities: list | None = None,
    has_pii: bool = False,
    suggested_title: str = "Test Title",
    suggested_scope: str = "team",
    reasoning: str = "Clear knowledge-sharing pattern detected.",
) -> dict:
    import json as _json
    inner = {
        "content_type": content_type,
        "confidence": confidence,
        "should_save": should_save,
        "entities": entities or [],
        "has_pii": has_pii,
        "suggested_title": suggested_title,
        "suggested_scope": suggested_scope,
        "reasoning": reasoning,
    }
    return {
        "choices": [
            {"message": {"content": _json.dumps(inner)}}
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 50},
    }


def _make_mock_client(response_dict: dict) -> MagicMock:
    """Return a MagicMock AsyncClient whose .post() returns the given response."""
    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.json.return_value = response_dict
    mock_response.raise_for_status.return_value = None
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


# ── JSON Parsing ───────────────────────────────────────────────


class TestJSONParsing:
    """Unit tests for _parse_json_response."""

    def test_parse_direct_json(self):
        result = LLMDetector._parse_json_response('{"content_type":"answer","confidence":0.9}')
        assert result["content_type"] == "answer"
        assert result["confidence"] == 0.9

    def test_parse_markdown_fenced(self):
        result = LLMDetector._parse_json_response(
            '```json\n{"content_type":"decision","confidence":0.95}\n```'
        )
        assert result["content_type"] == "decision"
        assert result["confidence"] == 0.95

    def test_parse_markdown_no_lang(self):
        result = LLMDetector._parse_json_response(
            '```\n{"content_type":"chat","confidence":0.9}\n```'
        )
        assert result["content_type"] == "chat"

    def test_parse_json_embedded_in_text(self):
        result = LLMDetector._parse_json_response(
            'Here is the classification:\n\n{"content_type":"answer","confidence":0.8}'
        )
        assert result["content_type"] == "answer"
        assert result["confidence"] == 0.8

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError, match="Could not parse JSON"):
            LLMDetector._parse_json_response("not json at all")


# ── DetectionResult Conversion ─────────────────────────────────


class TestToDetectionResult:
    """Unit tests for _to_detection_result."""

    def test_answer(self):
        parsed = {
            "content_type": "answer",
            "confidence": 0.92,
            "should_save": True,
            "entities": [{"type": "technology", "value": "Postgres"}],
            "has_pii": False,
            "suggested_title": "Why We Use Postgres",
            "suggested_scope": "team",
            "reasoning": "Explains database choice with rationale.",
        }
        result = LLMDetector._to_detection_result(parsed)
        assert result.content_type == ContentType.ANSWER
        assert result.confidence == 0.92
        assert result.suggested_title == "Why We Use Postgres"
        assert result.suggested_scope == "team"
        assert result.has_pii is False
        assert len(result.entities) == 1
        assert result.entities[0]["value"] == "Postgres"

    def test_decision(self):
        parsed = {
            "content_type": "decision",
            "confidence": 0.88,
            "should_save": True,
            "entities": [
                {"type": "technology", "value": "GraphQL"},
                {"type": "technology", "value": "REST"},
            ],
            "has_pii": False,
            "suggested_title": "Decision: Use GraphQL",
            "suggested_scope": "department",
            "reasoning": "Architecture decision with trade-off analysis.",
        }
        result = LLMDetector._to_detection_result(parsed)
        assert result.content_type == ContentType.DECISION
        assert result.confidence == 0.88
        assert result.suggested_scope == "department"
        assert len(result.entities) == 2

    def test_chat_default(self):
        parsed = {
            "content_type": "chat",
            "confidence": 0.95,
        }
        result = LLMDetector._to_detection_result(parsed)
        assert result.content_type == ContentType.CHAT
        assert result.confidence == 0.95
        assert result.suggested_scope == "team"
        assert result.suggested_title == ""

    def test_invalid_scope_defaults_to_team(self):
        parsed = {
            "content_type": "answer",
            "confidence": 0.7,
            "suggested_scope": "global",
        }
        result = LLMDetector._to_detection_result(parsed)
        assert result.suggested_scope == "team"

    def test_pii_true(self):
        parsed = {"content_type": "chat", "confidence": 0.5, "has_pii": True}
        result = LLMDetector._to_detection_result(parsed)
        assert result.has_pii is True

    def test_confidence_clamped(self):
        parsed = {"content_type": "answer", "confidence": 1.5}
        result = LLMDetector._to_detection_result(parsed)
        assert result.confidence == 1.0

        parsed = {"content_type": "answer", "confidence": -0.3}
        result = LLMDetector._to_detection_result(parsed)
        assert result.confidence == 0.0

    def test_invalid_entities_filtered(self):
        parsed = {
            "content_type": "answer",
            "confidence": 0.8,
            "entities": [
                {"type": "technology", "value": "Docker"},
                "not_a_dict",
                {"wrong_key": "oops"},
                {"type": "system", "value": "auth-service"},
            ],
        }
        result = LLMDetector._to_detection_result(parsed)
        assert len(result.entities) == 2
        assert result.entities[0]["value"] == "Docker"
        assert result.entities[1]["value"] == "auth-service"

    def test_signal_includes_reasoning(self):
        parsed = {
            "content_type": "answer",
            "confidence": 0.9,
            "reasoning": "Clear explanatory pattern with concrete details.",
        }
        result = LLMDetector._to_detection_result(parsed)
        assert any("Clear explanatory pattern" in s for s in result.signals)


# ── LLMDetector with Mocked HTTP ───────────────────────────────


class TestLLMDetectorMocked:
    """Tests that mock the HTTP layer to simulate LLM responses."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        reset_llm_detector()
        yield
        reset_llm_detector()

    def make_detector(self, **overrides) -> LLMDetector:
        """Create a detector with a fake API key (will never be called in tests
        unless we set _client manually)."""
        config = LLMConfig(
            api_key="sk-test-fake",
            model="gpt-4o-mini",
            max_retries=0,
        )
        for k, v in overrides.items():
            setattr(config, k, v)
        return LLMDetector(config)

    @pytest.mark.asyncio
    async def test_classify_answer(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="answer",
            confidence=0.91,
            suggested_title="Postgres over MySQL rationale",
            reasoning="Detailed technical explanation with trade-offs.",
        ))

        result = await detector.detect(
            "The reason we use Postgres over MySQL is because we need "
            "JSONB support and full-text search. We evaluated both in 2022 "
            "and Postgres came out ahead on every benchmark."
        )
        assert result.content_type == ContentType.ANSWER
        assert result.confidence == 0.91
        assert result.suggested_title == "Postgres over MySQL rationale"
        assert result.has_pii is False

    @pytest.mark.asyncio
    async def test_classify_decision(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="decision",
            confidence=0.94,
            suggested_title="Decision: Use GraphQL for API",
            suggested_scope="department",
            entities=[
                {"type": "technology", "value": "GraphQL"},
                {"type": "technology", "value": "REST"},
            ],
        ))

        result = await detector.detect(
            "Decision: We will use GraphQL for the new API. We considered "
            "REST and gRPC. GraphQL was chosen because the frontend team "
            "needs flexible queries."
        )
        assert result.content_type == ContentType.DECISION
        assert result.confidence == 0.94
        assert result.suggested_scope == "department"
        assert any(e["value"] == "GraphQL" for e in result.entities)

    @pytest.mark.asyncio
    async def test_classify_chat(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="chat",
            confidence=0.95,
            should_save=False,
        ))

        result = await detector.detect("ok thanks, sounds good!")
        assert result.content_type == ContentType.CHAT

    @pytest.mark.asyncio
    async def test_classify_with_pii(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="chat",
            confidence=0.9,
            has_pii=True,
            reasoning="Contains email address in a personal context.",
        ))

        result = await detector.detect(
            "Please contact john.doe@gmail.com for access to the beta program "
            "and include your department ID in the subject line for tracking."
        )
        assert result.has_pii is True

    @pytest.mark.asyncio
    async def test_is_worth_saving_true(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="answer",
            confidence=0.85,
        ))

        should, result = await detector.is_worth_saving(
            "The deployment pipeline uses three stages: build, test, deploy. "
            "Always check the CI status before proceeding to production."
        )
        assert should is True
        assert result.content_type == ContentType.ANSWER

    @pytest.mark.asyncio
    async def test_is_worth_saving_false_low_confidence(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="answer",
            confidence=0.10,  # below 0.15 threshold
        ))

        should, result = await detector.is_worth_saving("Some ambiguous text.")
        assert should is False

    @pytest.mark.asyncio
    async def test_retry_on_http_error(self):
        detector = self.make_detector(max_retries=2)
        mock_client = MagicMock(spec=httpx.AsyncClient)

        # First two calls fail with HTTP error, third succeeds
        fail_response = MagicMock(spec=httpx.Response)
        fail_response.raise_for_status.side_effect = httpx.HTTPError("Server error")
        success_response = MagicMock(spec=httpx.Response)
        success_response.json.return_value = _make_llm_response(
            content_type="answer", confidence=0.91,
        )
        success_response.raise_for_status.return_value = None

        mock_post = AsyncMock()
        mock_post.side_effect = [fail_response, fail_response, success_response]
        mock_client.post = mock_post
        mock_client.aclose = AsyncMock()

        detector._client = mock_client
        result = await detector.detect(
            "We evaluated three database options and chose Postgres for its "
            "JSONB support and full-text search capabilities across our "
            "entire microservice platform."
        )
        assert result.content_type == ContentType.ANSWER
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_falls_back_to_regex(self):
        """When all retries fail, fall back to regex detection."""
        detector = self.make_detector(max_retries=1)
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client.aclose = AsyncMock()
        detector._client = mock_client

        result = await detector.detect(
            "The reason we use Postgres over MySQL is because we need "
            "JSONB support and full-text search. We evaluated both in 2022."
        )
        # Should have fallen back to regex, which detects this as ANSWER
        assert result.content_type == ContentType.ANSWER

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response())

        await detector.detect(
            "We use Docker for containerization because it provides consistent "
            "environments across development and production."
        )
        assert detector.stats["calls"] == 1
        assert detector.stats["hits"] == 1
        assert detector.stats["misses"] == 0

    def test_detect_sync(self):
        """detect_sync should work for simple cases (no async loop running)."""
        detector = self.make_detector()
        detector._client = _make_mock_client(_make_llm_response(
            content_type="answer", confidence=0.88
        ))

        result = detector.detect_sync(
            "We use Postgres because of its robust JSONB support and "
            "excellent full-text search capabilities."
        )
        assert result.content_type == ContentType.ANSWER
        assert result.confidence == 0.88


# ── Fallback Behavior (no API key) ─────────────────────────────


class TestFallbackBehavior:
    """LLMDetector falls back to regex when no API key is configured."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_llm_detector()
        yield
        reset_llm_detector()

    @pytest.mark.asyncio
    async def test_detect_falls_back_without_api_key(self):
        detector = LLMDetector(LLMConfig(api_key=None))
        assert detector.available is False

        result = await detector.detect(
            "The reason we use Postgres is because we need JSONB."
        )
        # Regex fallback should classify this as ANSWER
        assert result.content_type == ContentType.ANSWER

    @pytest.mark.asyncio
    async def test_detect_uses_regex_for_short_text_even_with_key(self):
        detector = LLMDetector(LLMConfig(api_key="sk-fake", min_content_length=100))

        result = await detector.detect("ok")
        # Too short — regex fallback detects CHAT
        assert result.content_type == ContentType.CHAT

    @pytest.mark.asyncio
    async def test_get_llm_detector_returns_none_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            reset_llm_detector()
            llm = get_llm_detector()
            assert llm is None

    @pytest.mark.asyncio
    async def test_detect_async_falls_back_to_regex(self):
        """detect_async() uses regex when no LLM is configured."""
        with patch.dict("os.environ", {}, clear=True):
            reset_llm_detector()
            result = await detect_async(
                "We decided to use Kubernetes for orchestration because "
                "it provides auto-scaling and self-healing."
            )
            assert result.content_type == ContentType.DECISION

    @pytest.mark.asyncio
    async def test_is_worth_saving_async_regex_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            reset_llm_detector()
            should, result = await is_worth_saving_async(
                "The deployment process requires three approvals before "
                "going to production. Never skip this step. We decided this "
                "after a security incident where an unauthorized deploy caused "
                "a three-hour outage. The root cause was traced to a missing "
                "approval check in the CI pipeline."
            )
            assert should is True
            assert result.content_type == ContentType.ANSWER


# ── Configuration ──────────────────────────────────────────────


class TestLLMConfig:
    """Tests for LLMConfig.from_env()."""

    def test_from_env_openai_defaults(self):
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "sk-test-key",
        }, clear=True):
            config = LLMConfig.from_env()
            assert config.api_key == "sk-test-key"
            assert config.model == "gpt-4o-mini"
            assert config.provider == "openai"

    def test_from_env_custom_prefix(self):
        with patch.dict("os.environ", {
            "THREADWEAVE_LLM_API_KEY": "sk-custom",
            "THREADWEAVE_LLM_BASE_URL": "https://llm.internal/v1",
            "THREADWEAVE_LLM_MODEL": "llama-3-70b",
        }, clear=True):
            config = LLMConfig.from_env()
            assert config.api_key == "sk-custom"
            assert config.base_url == "https://llm.internal/v1"
            assert config.model == "llama-3-70b"

    def test_from_env_threadweave_takes_priority(self):
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "sk-openai",
            "THREADWEAVE_LLM_API_KEY": "sk-threadweave",
        }, clear=True):
            config = LLMConfig.from_env()
            assert config.api_key == "sk-threadweave"


# ── API Ingest with LLM (integration) ──────────────────────────


class TestIngestWithLLM:
    """End-to-end tests for the ingest pipeline with mocked LLM."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_llm_detector()
        yield
        reset_llm_detector()

    @pytest.mark.asyncio
    async def test_ingest_uses_llm_when_configured(self):
        """When an API key is set, the ingest pipeline uses LLM detection."""
        from fastapi.testclient import TestClient
        from threadweave.api import app

        client = TestClient(app)

        with patch.dict("os.environ", {
            "THREADWEAVE_LLM_API_KEY": "sk-test",
        }, clear=True):
            reset_llm_detector()

            # Mock the LLM detector's HTTP client
            llm = get_llm_detector()
            assert llm is not None
            llm._client = _make_mock_client(_make_llm_response(
                content_type="answer",
                confidence=0.92,
                suggested_title="Postgres for JSONB",
                suggested_scope="team",
            ))

            resp = client.post("/api/v1/ingest", json={
                "content": (
                    "The reason we use Postgres over MySQL is because we need "
                    "JSONB support and full-text search. We evaluated both in "
                    "2022 and Postgres came out ahead on every benchmark."
                ),
                "source": "teams",
                "tenant_id": "acme-corp",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["should_save"] is True
            assert data["content_type"] == "answer"
            assert data["confidence"] == 0.92
            assert data["deduplicated"] is False

    @pytest.mark.asyncio
    async def test_ingest_with_pii_rejected_by_llm(self):
        """LLM-detected PII should be rejected by the pipeline."""
        from fastapi.testclient import TestClient
        from threadweave.api import app

        client = TestClient(app)

        with patch.dict("os.environ", {"THREADWEAVE_LLM_API_KEY": "sk-test"}):
            reset_llm_detector()
            llm = get_llm_detector()
            llm._client = _make_mock_client(_make_llm_response(
                content_type="answer",
                confidence=0.9,
                has_pii=True,
                reasoning="Contains personal email.",
            ))

            resp = client.post("/api/v1/ingest", json={
                "content": "Contact john.doe@gmail.com for personal inquiries.",
                "source": "email",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["should_save"] is False
            assert data["has_pii"] is True

    @pytest.mark.asyncio
    async def test_ingest_regex_fallback_without_api_key(self):
        """Without API key, existing regex detection still works in ingest."""
        from fastapi.testclient import TestClient
        from threadweave.api import app

        client = TestClient(app)

        with patch.dict("os.environ", {}, clear=True):
            reset_llm_detector()

            resp = client.post("/api/v1/ingest", json={
                "content": (
                    "Decision: We will use Kubernetes for all new services. "
                    "We evaluated ECS and Nomad — Kubernetes won due to "
                    "ecosystem maturity."
                ),
                "source": "slack",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["should_save"] is True
            assert data["content_type"] == "decision"


# ── System Prompt Quality ──────────────────────────────────────


class TestSystemPrompt:
    """Sanity checks on the system prompt."""

    def test_prompt_contains_classification_rules(self):
        assert "ANSWER" in SYSTEM_PROMPT
        assert "DECISION" in SYSTEM_PROMPT
        assert "QUESTION" in SYSTEM_PROMPT
        assert "CHAT" in SYSTEM_PROMPT

    def test_prompt_contains_pii_guidance(self):
        assert "PII" in SYSTEM_PROMPT
        assert "email" in SYSTEM_PROMPT.lower()

    def test_prompt_contains_scope_guidance(self):
        assert '"team"' in SYSTEM_PROMPT
        assert '"department"' in SYSTEM_PROMPT
        assert '"organization"' in SYSTEM_PROMPT

    def test_prompt_asks_for_json_only(self):
        assert "ONLY a valid JSON object" in SYSTEM_PROMPT
