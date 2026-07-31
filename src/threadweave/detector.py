# SPDX-License-Identifier: MIT
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
    # Technical action verbs — common in engineering emails
    r"applied\s+(?:the\s+)?(?:fix|patch|update|change|workaround)",
    r"(?:released|deployed|shipped?|rolled\s+out)\s+(?:as\s+)?(?:\w+\s+)?(?:v(?:ersion\s+)?)?",
    r"(?:resolved|implemented|merged|fixed|patched)",
    r"(?:identified|diagnosed|pinpointed|found)\s+the\s+(?:issue|problem|bug|root\s+cause)",
    r"(?:confirmed|verified|validated|tested)\s+(?:that\s+)?(?:the\s+)?(?:fix|issue|problem)",
    r"(?:updated|upgraded|patched|rolled\s+back)\s+(?:to|the)",
    r"(?:the\s+)?(?:fix|solution|resolution)\s+(?:is|was|involves|requires)",
    r"(?:completed|finished|done)[,:;]?\s+(?:the\s+)?",
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
    # Technical explanation patterns
    r"(?:correctly|properly|accurately)\s+(?:identified|diagnosed|pinpointed|reported)",
    r"(?:uncovered|discovered|realized|noticed)\s+that",
    r"(?:the\s+)?root\s+cause\s+(?:is|was|appears|seems|turned\s+out)",
    r"(?:reproduce[ds]?)\s+(?:the\s+)?(?:issue|bug|problem)\s+by",
    r"(?:turned\s+out\s+(?:to\s+be|that))",
    r"(?:as\s+it\s+turns?\s+out)",
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

# ── External source detection ────────────────────────────────────
# Patterns that indicate newsletters, marketing, or external content
# that should NOT be saved as organizational knowledge — even if
# it contains answer-like or decision-like language.

EXTERNAL_SOURCE_PATTERNS = [
    r"(?:unsubscribe|view\s+(?:in\s+(?:browser|your\s+browser)|online)|web\s+version)",
    r"(?:register\s+now|subscribe\s+(?:now|today|here)|sign\s+up\s+(?:now|today|here))",
    r"you(?:\s+are|'re)\s+receiving\s+this\s+(?:email|message)\s+because",
    r"(?:privacy\s+policy|terms\s+(?:of\s+)?(?:service|use))",
    r"(?:add\s+us\s+to\s+your\s+address\s+book|whitelist\s+us|safe\s+sender\s+list)",
    r"(?:forward\s+(?:to\s+a\s+)?friend|share\s+(?:this|with))",
    r"this\s+email\s+was\s+sent\s+to",
    r"(?:update\s+your\s+(?:preferences|subscription|profile)|manage\s+(?:your\s+)?preferences)",
    r"©\s*\d{4}",
    r"all\s+rights\s+reserved",
    r"view\s+this\s+email\s+(?:in\s+your\s+browser|online)",
    r"you\s+(?:signed\s+up|subscribed|opted\s+in)",
    r"(?:weekly|monthly|daily)\s+(?:digest|newsletter|update|roundup|briefing)",
    r"(?:marketing\s+email|promotional\s+(?:email|offer|message))",
    # System-generated notifications (HR platforms, automated digests, etc.)
    r"\bdigest\b",  # "Your daily digest", "Workday Peakon digest" etc.
    r"(?:employee|engagement|pulse)\s+(?:survey|voice|feedback|insight)",
    r"(?:approve?\s+hours?|timesheet|expense\s+report)",
    r"(?:your\s+)?(?:daily|weekly|monthly)\s+(?:inbox|summary|brief|round-?up)",
    r"do\s+not\s+reply\s+(?:to\s+this\s+(?:email|message)|directly)",
    r"(?:automated|automatic)\s+(?:email|notification|message|reminder)",
]

PII_PATTERNS: list[str] = [
    # High-precision PII patterns — conservative, international.
    # Each pattern was chosen to avoid false positives on:
    #   - Company names (no proper-name or suffix matching)
    #   - Version numbers / equipment codes (no bare digit sequences)
    #   - Workplace identifiers (ticket numbers, PR IDs, build numbers)
    #
    # Design principle: false positives are WORSE than false negatives
    # because has_pii=True REJECTS the ingest. When in doubt, don't match.
    #
    # ── Locale-independent structured patterns ──────────────────
    # Credit card: 4-4-4-4 with dash/space separators
    r'\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b',
    # IBAN (2-letter country code + 2 check digits + up to 30 alphanum)
    r'\b[A-Z]{2}\d{2}\s?[A-Z0-9]{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?[\d]{0,4}\b',
    # US SSN: xxx-xx-xxxx
    r'\b\d{3}-\d{2}-\d{4}\b',
    # Nordic personal ID (DDMMYY-XXXXX or YYMMDD-XXXXX): 6+5 or 6-5
    r'\b\d{6}[\s-]?\d{5}\b',
    #
    # ── Context-gated: national ID / SSN (multilingual) ───────
    r'(?i)(?:ssn|social.security|national.id|personnummer|fødselsnummer|'
    r'cpr.nummer|NINO|tax.id|steuer.id|numéro.fiscal|'
    r'codice.fiscale|número.de.identidad)\s*[:#]?\s*[A-Z0-9\s\-/]{6,20}',
    #
    # ── Context-gated: bank account (multilingual) ─────────────
    r'(?i)(?:account\s+(?:no|number|#|nr)|kontonummer|konto\s*(?:nr|nummer)?|'
    r'bankkonto|bank\s+account|Bankverbindung|IBAN|'
    r'RIB|numéro\s+de\s+compte|número\s+de\s+cuenta)\s*[:.]?\s*[\d.\s\-]{6,30}',
    #
    # ── Context-gated: passport (multilingual) ─────────────────
    r'(?i)(?:passport|pass|Reisepass|passeport|pasaporte|passaporto)\s*'
    r'(?:no|number|#|nr|num|nº|número)\s*[:.]?\s*[A-Z0-9]{5,12}',
    #
    # ── Context-gated: home/personal address (multilingual) ────
    r'(?i)(?:home\s+address|personal\s+address|private\s+address|'
    r'hjemmeadresse|privatadresse|bostedsadresse|folkeregistrert|'
    r'Privatadresse|Wohnadresse|Heimatadresse|'
    r'adresse\s+personnelle|adresse\s+privée|domicile|'
    r'dirección\s+personal|domicilio\s+particular|'
    r'indirizzo\s+privato|indirizzo\s+di\s+casa)\s*[:;]\s*\S',
    #
    # ── Context-gated: salary/compensation (multilingual) ──────
    r'(?i)(?:salary|compensation|wage|income|remuneration|'
    r'lønn|årslønn|månedslønn|'
    r'Gehalt|Vergütung|Lohn|'
    r'salaire|rémunération|'
    r'salario|remuneración|sueldo|'
    r'stipendio|retribuzione|salaris)\s*[:;]\s*[\d.,]+\s*'
    r'(?:NOK|kr|USD|EUR|GBP|CHF|SEK|DKK|JPY|AUD|CAD)\b',
    #
    # ── Medical / health information ───────────────────────────
    r'(?i)\b(?:diagnosis|diagnose|diagnóstico|diagnosi|diagnostic|'
    r'prescription|prescripción|prescrizione|Rezept|ordonnance|'
    r'patient\s+(?:record|file|history|akte|dossier|expediente)|'
    r'medical\s+(?:condition|record|history)|'
    r'health\s+(?:record|condition|history))\b',
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

    # PII check runs first — must catch PII regardless of text length.
    # A short text containing a credit card number is still PII.
    has_pii = any(re.search(p, text) for p in PII_PATTERNS)

    if len(text) < min_length:
        return DetectionResult(
            content_type=ContentType.CHAT,
            confidence=0.9,
            signals=["too_short"],
            has_pii=has_pii,
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
            score["reference"] += len(matches) * 0.10
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

    # ── External source penalty ───────────────────────────────
    # Newsletters and marketing emails often mimic answer/decision
    # language. If the email has clear external-source signals,
    # we override the classification to prevent false saves.
    external_matches = 0
    for pattern in EXTERNAL_SOURCE_PATTERNS:
        if re.search(pattern, text_lower):
            external_matches += 1

    if external_matches >= 2:
        # Strong newsletter/external signal — override to reference, won't save
        signals.append(f"external_source({external_matches})")
        return DetectionResult(
            content_type=ContentType.REFERENCE,
            confidence=0.90,
            signals=signals,
            entities=[],
            suggested_scope="team",
            suggested_title="",
            has_pii=False,
        )
    elif external_matches >= 1:
        # Weak signal — penalize but don't fully override
        score["answer"] *= 0.5
        score["decision"] *= 0.5
        signals.append(f"external_source({external_matches})")

    # ── Determine primary type ───────────────────────────────

    scored = [
        (ContentType.DECISION, score["decision"]),
        (ContentType.ANSWER, score["answer"]),
        (ContentType.QUESTION, score["question"]),
        (ContentType.REFERENCE, score["reference"]),
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    primary_type, primary_score = scored[0]

    # Override: if reference wins but the text contains substantive
    # decisions or explanations, promote the higher of the two.
    # This prevents technical emails with incidental URLs/links
    # from being classified as mere references.
    best_substantive = max(score["decision"], score["answer"])
    if primary_type == ContentType.REFERENCE and best_substantive >= 0.2:
        if score["decision"] >= score["answer"]:
            primary_type = ContentType.DECISION
            primary_score = score["decision"]
        else:
            primary_type = ContentType.ANSWER
            primary_score = score["answer"]

    confidence = min(primary_score, 1.0)

    entities = _extract_entities(text)

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
        and result.confidence >= 0.40
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
