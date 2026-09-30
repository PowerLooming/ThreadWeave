# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Signature stripping and identifier redaction for the ingest path.

The decision gate answers ``has_pii``. Until now that answer was destructive: the
ingest endpoint returned ``rejected_pii`` and the message was gone. Two findings
made that untenable.

First, a phone number is the natural trigger for the question, and a Norwegian
mobile and a Norwegian work mobile are the same shape, eight digits starting with
4 or 9. In a work mailbox "a phone number is present" therefore means "a
signature is present". Measured on 1,692 real messages: 18.1% carry a signature
marker, and a credential pattern would delete 6.4% of the mailbox outright.

Second, a calibration fit on 251 hand-labelled real messages found no usable
operating point at any bar: a bar set for 95% recall still cost 34% of the
negatives on the best backend, 61% on the encoder. Deletion needs a precision
this corpus shows no local backend can deliver.

So identifiers are redacted and the message is kept. The exception is content
whose substance *is* the identifiers, a pasted roster or an ID list, which
``is_identifier_only`` recognises structurally.

Both mechanisms are deliberately conservative. Dropping body text, or masking a
string that was never personal data, is worse than keeping a signature: the
first loses knowledge, the second corrupts it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

__all__ = [
    "SignatureStrip",
    "Redaction",
    "strip_signature",
    "redact_identifiers",
    "sanitise",
    "decode_entities",
    "is_identifier_only",
    "KIND_PATTERNS",
    "DEFAULT_KINDS",
]


# ── signature and disclaimer detection ───────────────────────

SIGNATURE_MARKERS = re.compile(
    r"(?im)^[ \t]*(?:"
    r"--[ \t]*$"
    r"|med vennlig hilsen|vennlig hilsen|beste hilsen|hilsen[ \t]*[,.]?$|mvh[ \t,.]*$"
    r"|best regards|kind regards|warm regards|regards[ \t,.]*$|sincerely[ \t,.]*[A-Za-z]*$"
    r"|yours (?:sincerely|faithfully)"
    r"|sendt fra (?:min )?(?:iphone|ipad|android|samsung)|sent from my (?:iphone|ipad|android)"
    r")"
)

DISCLAIMER_MARKERS = re.compile(
    r"(?i)(?:this (?:e-?mail|message|email) (?:and any attachments )?(?:is|are|may)"
    r"|the information (?:in|contained in) this"
    r"|confidentiality notice"
    r"|denne e-?posten"
    r"|denne meldingen (?:er|kan)"
    r"|informasjonen i denne"
    r"|if you (?:have )?received this (?:e-?mail|message) in error)"
)

_SIGNATURE_FEATURE = re.compile(
    r"(?i)(?:[\w.+-]+@[\w-]+\.[\w.]+"
    r"|(?:\+|00)\s?\d{2,3}[\s-]?\d{2,4}"
    r"|https?://|www\."
    r"|\b(?:as|asa|ab|gmbh|ltd|inc|llc|oy|plc)\b"
    r"|tlf|tel[:.]|mob[:.]|phone[:.])"
)

_MIN_BODY = 100          # never leave less than this much body behind
_MAX_BLOCK_LINES = 12    # a signature is a short block
_START_FRACTION = 0.6    # default window: the last 40% of the message
_STRONG_FEATURES = 2     # features that justify looking earlier


@dataclass(frozen=True)
class SignatureStrip:
    """Result of :func:`strip_signature`."""

    text: str
    stripped: bool
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.stripped


def _looks_like_signature(tail: list[str], strict: bool) -> bool:
    joined = "\n".join(tail)
    if len(tail) > _MAX_BLOCK_LINES:
        return False
    features = len(_SIGNATURE_FEATURE.findall(joined))
    if strict:
        return features >= _STRONG_FEATURES
    return features >= 1 or len(joined.strip()) < 400


def strip_signature(
    text: str,
    *,
    keep_disclaimer: bool = False,
    min_body: int = _MIN_BODY,
) -> SignatureStrip:
    """Remove a trailing signature or disclaimer block.

    Two tiers, because the two failure modes are not equally bad. Losing body
    text is worse than keeping boilerplate, so by default a marker is only
    honoured in the last 40% of the message (measured: strips 5.7% of a real
    mailbox). A marker further up is honoured only when the block after it is
    unambiguously a signature, carrying at least two contact or company
    features. In both cases at least ``min_body`` characters must remain.

    Anything doubtful is returned unchanged.
    """
    if not text:
        return SignatureStrip(text, False, "empty")
    lines = text.split("\n")
    if len(lines) < 2:
        return SignatureStrip(text, False, "too short")
    default_window = int(len(lines) * (1 - _START_FRACTION))

    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        is_disclaimer = bool(DISCLAIMER_MARKERS.search(line))
        is_signature = bool(SIGNATURE_MARKERS.search(line))
        if not (is_disclaimer or is_signature):
            continue
        if is_disclaimer and keep_disclaimer and not is_signature:
            continue

        in_default_window = idx >= default_window
        tail = lines[idx + 1:]
        if not _looks_like_signature(tail, strict=not in_default_window):
            return SignatureStrip(text, False, "marker found but block is not a signature")

        body = "\n".join(lines[:idx]).rstrip()
        if len(body) < min_body:
            return SignatureStrip(text, False, "stripping would leave too little body")
        kind = "disclaimer" if is_disclaimer else "signature"
        where = "last 40%" if in_default_window else "earlier, strong block"
        return SignatureStrip(body, True,
                              f"{kind} at line {idx} ({where}, {len(tail)} lines removed)")

    return SignatureStrip(text, False, "no signature found")


# ── identifiers ──────────────────────────────────────────────

# Ordered by specificity: a mobile inside a longer digit run is not a mobile,
# and a card-like run should win over the national-id shape it also matches.
# A value may be a bare pattern (the whole match is replaced) or a
# ``(pattern, group)`` pair, for shapes where only part of the match is the
# identifier and the rest has to survive.
KIND_PATTERNS: dict[str, re.Pattern | tuple[re.Pattern, int]] = {
    "national_id": re.compile(r"(?<![\d.])\d{6}[\s-]?\d{5}(?![\d.])"),
    "bank_account": re.compile(r"(?<!\d)\d{4}[.\s]\d{2}[.\s]\d{5}(?!\d)"),
    "card": re.compile(r"(?<![\d.])(?:\d{4}[\s-]?){3}\d{4}(?![\d.])"),
    "mobile_no": re.compile(r"\+\s?47[\s-]?[49]\d{2}[\s-]?\d{2}[\s-]?\d{3}(?!\d)"),
    "mobile_bare": re.compile(
        r"(?<![0-9A-Za-z+])(?:[49]\d{2}[\s-]?\d{2}[\s-]?\d{3})(?!\d)"
    ),
    "postal_address": re.compile(
        r"\b[A-ZÆØÅ][a-zæøå]+"
        r"(?:veien|vegen|vei|gata|gate|gt|plass|stien|alle|allé|lien|lia|bakken|svingen"
        r"|kroken|fjellet|haugen|neset|tunet|jordet|åsen|sletta|svingen)\s+\d+[A-Za-z]?"
    ),
    "postal_code_place": re.compile(
        r"(?<![\d.,])\b(?!19\d\d\b|20\d\d\b)\d{4}\s+[A-ZÆØÅ][a-zæøå]{2,}"
    ),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    # A field name that identifies a person, followed by its value. The label
    # survives and only the value is masked, so the entry still explains itself.
    # Measured need: a union newsletter carrying "Medlemsnummer: 51764694" scored
    # 0.00 with every backend and matched no pattern, because it is a labelled
    # value rather than a shape. Deliberately does not cover order or case
    # numbers: those identify a transaction, not a person.
    "labelled_identifier": (
        re.compile(
            r"(?i)\b(?:medlems[\s-]?(?:nummer|nr|kapsnummer|ident)|"
            r"kunde[\s-]?(?:nummer|nr|id)|kundennummer)\b\s*[:#]?\s*(\d{4,})"
        ),
        1,
    ),
}

# A number that follows one of these is a transaction reference, not a person's
# identifier. Measured need: "Fakturanr 99887766" is eight digits starting with 9
# and reads as a Norwegian mobile to a shape pattern. The guard keeps shapes from
# masking invoice, order, case and parcel numbers.
TXN_CONTEXT = re.compile(
    r"(?i)(?:ordre|order|faktura|invoice|sak|case|bilag|kvittering|referanse|ref|"
    r"pakke|sporing|tracking|serie|batch)(?:nr|no|nummer|id)?[:#\s-]{0,4}$"
)

# Kinds that are pure shapes and therefore need the guard. A labelled identifier
# carries its own label, so it is not one of them.
SHAPE_KINDS: frozenset[str] = frozenset({
    "national_id", "bank_account", "card", "mobile_no", "mobile_bare",
})

# A signature block is publishing information and a national ID is not, so the
# default set is the identifiers that must not sit in stored content. Work
# contact details are deliberately absent.
DEFAULT_KINDS: tuple[str, ...] = (
    "national_id",
    "bank_account",
    "card",
    "mobile_no",
    "mobile_bare",
    "postal_address",
    "postal_code_place",
    "labelled_identifier",
)

# Noise that makes naive patterns useless on real mail. Measured: of 348
# national-ID-shaped hits in one mailbox, every sampled one sat inside a
# tracking URL; every sampled "card" was a marketing parameter.
_URLISH = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w.-]+\.(?:com|no|org|net|dk|se|io|nu|co\.uk)\b\S*"
    r"|\b[A-Za-z0-9+/=_-]{24,}\b"
)


@dataclass(frozen=True)
class Redaction:
    """Result of :func:`redact_identifiers`."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))


def decode_entities(text: str) -> str:
    """Decode HTML entities before matching.

    ``&#43;`` hides the ``+`` of a ``+47`` number from every pattern, so an
    encoded phone number is invisible to a naive matcher.
    """
    return html.unescape(text) if "&" in text else text


def _inside(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(lo <= pos < hi for lo, hi in spans)


def redact_identifiers(
    text: str,
    *,
    kinds: tuple[str, ...] | list[str] | None = None,
    mask_noise: bool = True,
    decode: bool = True,
) -> Redaction:
    """Replace identifiers with typed placeholders.

    ``[mobile_no]`` rather than a redaction bar, so the shape of what was
    removed survives in the entry and a reader is not left guessing. Counts are
    returned so the caller can audit what it stripped; the values never are.
    """
    if not text:
        return Redaction(text, {})
    source = decode_entities(text) if decode else text
    selected = tuple(kinds) if kinds else DEFAULT_KINDS
    spans = [m.span() for m in _URLISH.finditer(source)] if mask_noise else []

    matches: list[tuple[int, int, str]] = []
    for kind in selected:
        spec = KIND_PATTERNS.get(kind)
        if spec is None:
            continue
        pattern, group = spec if isinstance(spec, tuple) else (spec, 0)
        for mt in pattern.finditer(source):
            start, end = mt.span(group)
            if start < 0 or end <= start:
                continue
            if mask_noise and _inside(spans, start):
                continue
            if kind in SHAPE_KINDS and TXN_CONTEXT.search(source[max(0, start - 24):start]):
                continue
            matches.append((start, end, kind))

    # longest match wins at the same start, then no overlaps
    matches.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    kept: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, kind in matches:
        if start >= last_end:
            kept.append((start, end, kind))
            last_end = end

    counts: dict[str, int] = {}
    out = source
    for start, end, kind in reversed(kept):
        counts[kind] = counts.get(kind, 0) + 1
        out = f"{out[:start]}[{kind}]{out[end:]}"
    return Redaction(out, counts)


def is_identifier_only(text: str, *, min_ratio: float = 0.5, min_total: int = 3) -> bool:
    """True when a message is essentially a list of identifiers.

    The one case where redaction leaves nothing worth keeping: a pasted roster,
    an ID list, a dump of account numbers. Structural, not a model confidence:
    at least ``min_total`` identifiers and at least ``min_ratio`` of the
    non-space characters inside them.
    """
    red = redact_identifiers(text)
    if red.total < min_total:
        return False
    stripped_len = len("".join(text.split()))
    if not stripped_len:
        return False
    placeholder_len = sum(len(f"[{k}]") for k, v in red.counts.items() for _ in range(v))
    return placeholder_len / stripped_len >= min_ratio


def sanitise(
    text: str,
    *,
    kinds: tuple[str, ...] | list[str] | None = None,
    strip: bool = True,
) -> tuple[str, Redaction, SignatureStrip]:
    """Strip a trailing signature, then redact identifiers.

    Returns the text to store, the redaction record, and what the stripper did.
    """
    sig = strip_signature(text) if strip else SignatureStrip(text, False, "disabled")
    return sig.text, redact_identifiers(sig.text, kinds=kinds), sig
