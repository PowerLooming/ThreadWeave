# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Language identification without a model.

The decision layer needs to know the message language, because non-English
content is translated at ingest. An entailment classifier is the wrong tool for
that question: measured against bge-m3-zeroshot-v2.0-c, a Choice over language
names scored the correct language at 0.17 to 0.23 while picking wrong winners,
and the smaller English model was no better. When a provider cannot answer the
language question, this detector answers it instead.

Method, deliberately simple and dependency free: script detection for non-Latin
alphabets, then function-word profiles with character hints for Latin scripts,
scored as the share of tokens that appear in each profile. Confidence is the
margin between the best and the runner-up profile, so a text that looks equally
like two languages reports low confidence rather than a coin flip.

It is a router, not a linguist. A wrong answer costs a translation attempt,
never data, which is why the gate only acts above its language threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = ["LanguageGuess", "identify", "SUPPORTED_LANGUAGES"]


@dataclass
class LanguageGuess:
    language: str  # ISO 639-1, or "" when nothing scored
    confidence: float  # 0.0 - 1.0, margin between best and runner-up
    runner_up: str = ""


# ── Script detection (non-Latin) ─────────────────────────────

_SCRIPT_RANGES: List[Tuple[str, Tuple[Tuple[int, int], ...]]] = [
    ("ko", ((0xAC00, 0xD7A3), (0x1100, 0x11FF))),  # Hangul
    ("ja", ((0x3040, 0x30FF),)),  # hiragana / katakana
    ("zh", ((0x4E00, 0x9FFF),)),  # CJK unified ideographs
    ("ru", ((0x0400, 0x04FF),)),  # Cyrillic (resolved further below)
    ("ar", ((0x0600, 0x06FF),)),
    ("hi", ((0x0900, 0x097F),)),  # Devanagari
]

_CYRILLIC_UK = set("іїєґІЇЄҐ")
_LATIN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿŁłŚśŹźŻżĄąĘęĆćŃńŞşĞğİı]")


def _script_guess(text: str) -> Optional[Tuple[str, float]]:
    counts: Dict[str, int] = {}
    total = 0
    for ch in text:
        code = ord(ch)
        if code < 0x80 or _LATIN.match(ch):
            continue
        total += 1
        for lang, ranges in _SCRIPT_RANGES:
            if any(lo <= code <= hi for lo, hi in ranges):
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts or total < 4:
        return None
    lang, hits = max(counts.items(), key=lambda kv: kv[1])
    share = hits / max(total, 1)
    if lang == "ru" and any(ch in _CYRILLIC_UK for ch in text):
        lang = "uk"
    if lang == "zh":
        # Kana alongside Han means Japanese, not Chinese.
        if any(0x3040 <= ord(ch) <= 0x30FF for ch in text):
            lang = "ja"
        else:
            # Japanese text frequently carries kana; Han alone stays ambiguous.
            share *= 0.8
    return lang, min(1.0, share)


# ── Function-word profiles (Latin scripts) ───────────────────

_PROFILES: Dict[str, set] = {
    "en": {"the", "and", "of", "to", "is", "are", "we", "that", "this", "with",
           "for", "not", "have", "will", "it", "our", "from", "they", "was", "be"},
    "no": {"ikke", "også", "hvor", "hva", "jeg", "vi", "er", "det", "som", "til",
           "med", "av", "på", "blir", "skal", "og", "å", "ikkje", "hvordan", "etter"},
    "da": {"ikke", "også", "hvor", "hvad", "jeg", "vi", "er", "det", "som", "til",
           "med", "af", "på", "bliver", "skal", "og", "at", "hvordan", "efter"},
    "sv": {"inte", "också", "var", "vad", "jag", "vi", "är", "det", "som", "till",
           "med", "av", "på", "blir", "ska", "och", "att", "hur", "efter"},
    "de": {"und", "der", "die", "das", "ist", "nicht", "auch", "wir", "mit", "für",
           "von", "dass", "sich", "ein", "eine", "wird", "auf", "den", "dem"},
    "nl": {"en", "de", "het", "is", "niet", "ook", "wij", "met", "voor", "van",
           "dat", "een", "wordt", "zijn", "op", "maar", "deze"},
    "fr": {"et", "le", "la", "les", "des", "est", "ne", "pas", "nous", "avec",
           "pour", "que", "qui", "dans", "une", "sont", "sur", "au", "ce"},
    "es": {"y", "el", "la", "los", "de", "es", "no", "también", "nosotros", "con",
           "para", "que", "una", "en", "del", "por", "las", "pero"},
    "it": {"e", "il", "la", "di", "è", "non", "anche", "noi", "con", "per", "che",
           "una", "nel", "del", "sono", "ma", "come"},
    "pt": {"e", "o", "a", "de", "é", "não", "também", "nós", "com", "para", "que",
           "uma", "em", "do", "os", "as", "mas", "por"},
    "pl": {"i", "nie", "jest", "to", "my", "z", "na", "do", "że", "się", "dla",
           "oraz", "ale", "jak", "przez"},
    "fi": {"ja", "on", "ei", "myös", "me", "kanssa", "että", "se", "ovat",
           "varten", "mutta", "kun", "niin", "koska", "jos", "sekä", "vielä",
           "kaikki", "tämä", "nämä", "kunnes", "jälkeen"},
    "tr": {"ve", "bir", "de", "da", "bu", "için", "ile", "değil", "olarak", "var",
           "ama", "çok", "daha"},
}

_CHAR_HINTS: Dict[str, Tuple[str, float]] = {
    "de": ("ß", 0.10),
    "nl": ("ij", 0.08),
    "fr": ("é", 0.05),
    "es": ("ñ", 0.08),
    "pt": ("ã", 0.08),
    "pl": ("ł", 0.10),
    "tr": ("ğ", 0.10),
    "sv": ("ä", 0.04),
    "fi": ("ä", 0.02),
    "no": ("ø", 0.06),
    "da": ("ø", 0.06),
}

# Short messages often carry no function words, so a few language-typical
# substrings are scored too. Heuristic by construction: they break ties between
# near-identical profiles (Finnish versus Swedish, Norwegian versus Danish),
# they do not decide a language on their own.
_SUBSTRING_HINTS: Dict[str, Tuple[Tuple[str, float], ...]] = {
    "fi": (("ää", 0.10), ("nen", 0.05), ("ssa", 0.05), ("sta", 0.04)),
    "sv": (("ck", 0.04), ("och", 0.06)),
    "da": (("bliver", 0.05), ("hvad", 0.05), (" af ", 0.05)),
    "no": (("blir", 0.05), ("hva", 0.04), (" av ", 0.04)),
}

# Below this many words there is not enough evidence to name a language.
_MIN_TOKENS = 3

_UMLAUT_HINTS = {"ä", "ö", "ü"}
_NORDIC_HINTS = {"ø", "æ", "å"}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
SUPPORTED_LANGUAGES = tuple(_PROFILES) + ("ru", "uk", "ar", "hi", "zh", "ja", "ko")


def _latin_scores(text: str) -> Dict[str, float]:
    tokens = [token.lower() for token in _WORD.findall(text)]
    if len(tokens) < _MIN_TOKENS:
        return {}
    lowered = text.lower()
    scores: Dict[str, float] = {}
    for lang, words in _PROFILES.items():
        hits = sum(1 for token in tokens if token in words)
        scores[lang] = hits / len(tokens)
    for lang, (marker, weight) in _CHAR_HINTS.items():
        if marker in lowered and lang in scores:
            scores[lang] += weight
    for lang, hints in _SUBSTRING_HINTS.items():
        if lang in scores:
            for substring, weight in hints:
                if substring in lowered:
                    scores[lang] += weight
                    break
    if not _NORDIC_HINTS & set(lowered):
        # No æ/ø/å: not a Nordic language, unless the function words say so
        # anyway (some writers drop diacritics entirely).
        for lang in ("no", "da", "sv"):
            scores[lang] *= 0.6
    if any(ch in lowered for ch in _UMLAUT_HINTS):
        for lang in ("de", "sv", "fi"):
            scores[lang] += 0.04
    return scores


def identify(text: str, min_confidence: float = 0.0) -> LanguageGuess:
    """Guess the language of ``text``.

    Returns a guess with a confidence derived from the margin between the two
    best candidates, so genuinely ambiguous text reports low confidence. An
    empty language means nothing scored; the caller decides what to do with it.
    """
    if not text or not text.strip():
        return LanguageGuess(language="", confidence=0.0)

    script = _script_guess(text)
    if script is not None:
        lang, share = script
        return LanguageGuess(language=lang, confidence=round(min(1.0, share), 4))

    scores = {lang: score for lang, score in _latin_scores(text).items() if score > 0}
    if not scores:
        return LanguageGuess(language="", confidence=0.0)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_lang, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    confidence = (best - second) / best if best > 0 else 0.0
    if confidence < min_confidence:
        return LanguageGuess(language="", confidence=round(confidence, 4), runner_up=best_lang)
    return LanguageGuess(
        language=best_lang,
        confidence=round(confidence, 4),
        runner_up=ordered[1][0] if len(ordered) > 1 else "",
    )
