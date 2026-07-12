# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Detection engine — determines whether text is "worth saving" as organizational knowledge.

Heuristic-based (no LLM required for classification). Classifies text into:
- ANSWER: An explanation, instruction, or decision that should be preserved
- QUESTION: A question someone asked (context, but not the knowledge itself)
- CHAT: Casual conversation, not worth saving
- DECISION: An explicit decision with rationale
- REFERENCE: A link, pointer, or reference to external resource
"""

import re
from dataclasses import dataclass, field
from enum import Enum


class ContentType(Enum):
    ANSWER = "answer"
    QUESTION = "question"
    CHAT = "chat"
    DECISION = "decision"
    REFERENCE = "reference"


@dataclass
class DetectionResult:
    content_type: ContentType
    confidence: float  # 0.0 - 1.0
    signals: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    suggested_scope: str = "team"  # team, department, organization
    suggested_title: str = ""
    has_pii: bool = False


# ── Signal patterns ──────────────────────────────────────────────

DECISION_PATTERNS = [
    r"let['\u2019]?s?\s+(?:go\s+with|use|switch\s+to|stick\s+with)",
    r"(?:decided|decision|chose|chosen|settled\s+on)",
    r"(?:we['\u2019]re\s+going\s+with|we\s+will\s+use|the\s+plan\s+is)",
    r"(?:conclusion|resolution|verdict)",
    r"(?:approved|rejected|accepted|declined)",
    r"(?:architecture\s+decision|ADR)",
]

ANSWER_PATTERNS = [
    r"(?:the\s+reason\s+(?:is|was|for)|because|this\s+is\s+because)",
    r"(?:you\s+(?:need\s+to|should|can|must|have\s+to))",
    r"(?:the\s+(?:trick|key|issue|problem|solution)\s+(?:is|was))",
    r"(?:here[\u2019']?s?\s+(?:how|why|what|the))",
    r"(?:always|never|usually|typically|generally)",
    r"(?:remember\s+that|keep\s+in\s+mind|note\s+that)",
    r"(?:in\s+my\s+experience|what\s+I[\u2019']ve\s+found)",
    r"(?:the\s+(?:pattern|rule|convention|standard|policy)\s+(?:is|says))",
    r"(?:we\s+use\s+\w+(?:\s+(?:for|to|as|because|since)))",  # Descriptive
    r"(?:this\s+is\s+(?:a|the)\s+.+\s+(?:policy|standard|practice|approach))",  # Policy
]

QUESTION_PATTERNS = [
    r"^(?:how|what|why|when|where|who|can|could|would|should|is\s+it|are\s+there|do\s+you|does\s+it)",
    r"\?\s*$",
    r"(?:any\s+(?:idea|thoughts|suggestions|clue)\??)",
    r"(?:does\s+anyone\s+know|has\s+anyone)",
]

REFERENCE_PATTERNS = [
    r"(?:https?://|www\.)\S+",
    r"(?:see|check|look\s+at)\s+(?:the\s+)?(?:docs?|wiki|confluence|notion|readme)",
    r"(?:link\s+(?:to|for)|here[\u2019']?s?\s+(?:the\s+)?(?:link|url|reference))",
    r"(?:ticket|issue|PR|pull\s+request)\s+(?:#|number\s+)?\d+",
]

PII_PATTERNS = [
    r'\b\d{3}[-.]?\d{2}[-.]?\d{4}\b',        # SSN
    r'\b(?:\\+\\d{1,2}\\s?)?\\(?\d{3}\\)?[\\s.-]?\d{3}[\\s.-]?\d{4}\b',  # Phone
]

# ── Technologies to detect ───────────────────────────────────────

KNOWN_TECHS = [
    "Docker", "Kubernetes", "Postgres", "MySQL", "Redis", "Kafka",
    "GraphQL", "REST", "gRPC", "React", "Python", "Java", "Go",
    "AWS", "GCP", "Azure", "Terraform", "Jenkins", "GitHub", "GitLab",
    "TLS", "SSL", "OAuth", "JWT", "SAML", "LDAP", "SQLite", "Qdrant",
]


def detect(text: str, min_length: int = 50) -> DetectionResult:
    """Classify text and determine if it should be saved as organizational knowledge.

    Args:
        text: The text to classify (email body, Slack message, PR comment, etc.)
        min_length: Minimum character length for detailed analysis

    Returns:
        DetectionResult with content type, confidence, and extracted metadata.
    """
    signals: list[str] = []
    score = {"answer": 0.0, "decision": 0.0, "question": 0.0, "reference": 0.0}
    text_lower = text.lower()

    if len(text) < min_length:
        return DetectionResult(
            content_type=ContentType.CHAT,
            confidence=0.9,
            signals=["too_short"],
        )

    # ── Score each category ──────────────────────────────────

    for pattern in DECISION_PATTERNS:
        matches = re.findall(pattern, text_lower)
        if matches:
            score["decision"] += len(matches) * 0.25
            signals.append(f"decision: {matches[0][:40]}")

    for pattern in ANSWER_PATTERNS:
        matches = re.findall(pattern, text_lower)
        if matches:
            score["answer"] += len(matches) * 0.20
            signals.append(f"answer: {matches[0][:40]}")

    for pattern in QUESTION_PATTERNS:
        matches = re.findall(pattern, text_lower)
        if matches:
            score["question"] += len(matches) * 0.30
            signals.append(f"question: {matches[0][:40]}")

    for pattern in REFERENCE_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            score["reference"] += len(matches) * 0.20
            signals.append("reference")

    # ── Structural signals ──────────────────────────────────

    if len(text) > 500:
        score["answer"] += 0.15
        signals.append("long_form")

    if text.count("\n") > 3:
        score["answer"] += 0.10
        signals.append("structured")

    if re.search(r"^\s*(?:\d+[.)]|[-*+])\s", text, re.MULTILINE):
        score["answer"] += 0.10
        signals.append("list_format")

    # ── Determine primary type ───────────────────────────────

    scored = [
        (ContentType.DECISION, score["decision"]),
        (ContentType.ANSWER, score["answer"]),
        (ContentType.QUESTION, score["question"]),
        (ContentType.REFERENCE, score["reference"]),
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    primary_type, primary_score = scored[0]

    confidence = min(primary_score, 1.0)

    entities = _extract_entities(text)
    has_pii = any(re.search(p, text) for p in PII_PATTERNS)

    if confidence < 0.15:
        return DetectionResult(
            content_type=ContentType.CHAT,
            confidence=0.8,
            signals=["no_strong_signal"],
            entities=entities,
            has_pii=has_pii,
        )

    scope = "team"
    if "company" in text_lower or "org" in text_lower or "everyone" in text_lower:
        scope = "organization"
    elif "department" in text_lower or "division" in text_lower:
        scope = "department"

    title = _suggest_title(text, primary_type)

    return DetectionResult(
        content_type=primary_type,
        confidence=confidence,
        signals=signals,
        entities=entities,
        suggested_scope=scope,
        suggested_title=title,
        has_pii=has_pii,
    )


def _extract_entities(text: str) -> list[dict]:
    """Extract named entities from text (regex-based)."""
    entities: list[dict] = []

    system_pattern = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text)
    for s in set(system_pattern):
        entities.append({"type": "system", "value": s})

    for tech in KNOWN_TECHS:
        if re.search(r'\b' + re.escape(tech) + r'\b', text, re.IGNORECASE):
            entities.append({"type": "technology", "value": tech})

    company_pattern = re.findall(
        r'\b([A-Z][a-z]+(?:\s+(?:Corp|Inc|Ltd|LLC|GmbH|AS|ASA)))', text
    )
    for c in set(company_pattern):
        entities.append({"type": "organization", "value": c})

    return entities


def _suggest_title(text: str, content_type: ContentType) -> str:
    """Generate a suggested title from the first sentence."""
    first_sentence = text.split(".")[0][:100].strip()
    if len(first_sentence) > 10:
        return first_sentence

    if content_type == ContentType.DECISION:
        return "Architecture Decision"
    elif content_type == ContentType.ANSWER:
        return "Knowledge Entry"
    return ""


def is_worth_saving(text: str) -> tuple[bool, DetectionResult]:
    """Quick check: should this text be offered for saving?

    Returns (should_prompt, result).
    Only returns True for ANSWER and DECISION types with high confidence.
    """
    result = detect(text)
    should_prompt = (
        result.content_type in (ContentType.ANSWER, ContentType.DECISION)
        and result.confidence >= 0.2
    )
    return should_prompt, result


# ── Async detection (auto-selects LLM if configured) ──────────


async def detect_async(text: str, min_length: int = 50) -> DetectionResult:
    """Async version of detect() — tries LLM first, regex fallback.

    Uses the LLMDetector if an API key is configured AND the text is
    long enough to justify the API call. Otherwise falls back to the
    regex-based detect().

    To enable LLM detection, set one of:
        THREADWEAVE_LLM_API_KEY / OPENAI_API_KEY
        + optionally THREADWEAVE_LLM_BASE_URL / THREADWEAVE_LLM_MODEL
    """
    try:
        from threadweave.llm_detector import get_llm_detector
        llm = get_llm_detector()
        if llm is not None:
            return await llm.detect(text, min_length)
    except Exception:
        pass
    return detect(text, min_length)


async def is_worth_saving_async(text: str) -> tuple[bool, DetectionResult]:
    """Async version of is_worth_saving()."""
    try:
        from threadweave.llm_detector import get_llm_detector
        llm = get_llm_detector()
        if llm is not None:
            return await llm.is_worth_saving(text)
    except Exception:
        pass
    return is_worth_saving(text)
