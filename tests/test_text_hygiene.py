# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for signature stripping and identifier redaction."""

from __future__ import annotations

from threadweave.text_hygiene import (
    Redaction,
    SignatureStrip,
    decode_entities,
    is_identifier_only,
    redact_identifiers,
    sanitise,
    strip_signature,
)

BODY = (
    "Vi besluttet å flytte sesjonscachen til Redis fordi lasten ble for høy mot "
    "slutten av dagen, og vi mister sesjoner ved omstart. Beslutningen gjelder fra "
    "neste sprint og er dokumentert i arkitekturnotatet."
)


# ── signature stripping ──────────────────────────────────────


def test_strips_a_trailing_signature_and_keeps_the_body():
    text = f"{BODY}\n\nMvh\nHarald Daltveit\nTlf: 98251606\nharald@example.no"
    result = strip_signature(text)
    assert result.stripped
    assert "98251606" not in result.text
    assert result.text.startswith("Vi besluttet")
    assert "arkitekturnotatet" in result.text


def test_english_and_norwegian_markers_both_match():
    for marker in ("Best regards", "Med vennlig hilsen", "Mvh", "Hilsen", "--"):
        text = f"{BODY}\n\n{marker}\nKari Nordmann\nkari@example.no"
        assert strip_signature(text).stripped, marker


def test_word_in_a_sentence_is_not_a_marker():
    text = (
        "Jeg sendte hilsen til kunden i går, og de svarte at avtalen er signert av "
        "begge parter. Videre følger en gjennomgang av leveransen og neste steg for "
        "teamet."
    )
    assert not strip_signature(text).stripped


def test_disclaimer_is_removed():
    text = (
        f"{BODY}\n\nDenne e-posten kan inneholde konfidensiell informasjon. "
        "Hvis du har mottatt den ved en feil, vennligst slett den."
    )
    result = strip_signature(text)
    assert result.stripped
    assert "konfidensiell" not in result.text


def test_keep_disclaimer_leaves_it_in_place():
    text = f"{BODY}\n\nDenne e-posten kan inneholde konfidensiell informasjon."
    result = strip_signature(text, keep_disclaimer=True)
    assert not result.stripped


def test_refuses_when_it_would_leave_too_little_body():
    text = "Kort melding.\n\nMvh\nHarald\nharald@example.no"
    result = strip_signature(text)
    assert not result.stripped
    assert result.text == text


def test_untouched_text_is_returned_byte_for_byte():
    result = strip_signature(BODY)
    assert not result.stripped
    assert result.text == BODY


def test_marker_without_signature_features_is_left_alone():
    text = f"{BODY}\n\nHilsen\n" + ("og her fortsetter det med vanlig tekst. " * 30)
    assert not strip_signature(text).stripped


def test_a_strong_block_earlier_in_the_message_is_still_stripped():
    lines = [f"Innhold linje {i} som må bevares." for i in range(24)]
    text = "\n".join(lines) + "\n\nMed vennlig hilsen\nKari\nkari@example.no\nTlf: 98251606"
    result = strip_signature(text)
    assert result.stripped
    assert "Innhold linje 0" in result.text
    assert "kari@example.no" not in result.text


# ── redaction ────────────────────────────────────────────────


def test_redacts_each_identifier_kind():
    cases = {
        "national_id": "Fødselsnummer 01019012345 er registrert.",
        "bank_account": "Mitt kontonummer er 3569.15.05322.",
        "card": "Kort 4111 1111 1111 1111 er reservert.",
        "mobile_no": "Ring meg på +47 954 94 679 i morgen.",
        "mobile_bare": "Tlf: 98251606",
        "postal_address": "Sendes til Apeltunlien 13A",
        "postal_code_place": "Adresse: 5238 Rådal",
    }
    for kind, text in cases.items():
        result = redact_identifiers(text)
        assert kind in result.counts, f"{kind} not detected in {text!r}"
        assert f"[{kind}]" in result.text
        assert result.text != text


def test_does_not_redact_a_tracking_url():
    text = "Se https://example.com/?qs=bc9b650f7177890b4cedfbb72c7397d942fa90d09634067562"
    result = redact_identifiers(text)
    assert result.total == 0


def test_does_not_redact_years():
    assert redact_identifiers("I 2016 og 2022 gikk dette bra.").total == 0


def test_does_not_redact_a_hash_fragment():
    """Eight digits inside a longer alphanumeric run are not a phone number."""
    text = "Ordre-referanse x9x47594832x ligger i systemet."
    assert redact_identifiers(text).total == 0


def test_entity_encoded_number_is_caught():
    text = "Ring &#43;4795494679"
    assert "mobile_no" in redact_identifiers(text).counts
    assert decode_entities(text).startswith("Ring +47")


def test_work_contact_details_are_not_in_the_default_set():
    text = "Kontakt: post@example.com, tlf 22 33 44 55"
    result = redact_identifiers(text)
    assert "email" not in result.counts
    assert result.total == 0


def test_email_can_be_selected_explicitly():
    result = redact_identifiers("post@example.com", kinds=("email",))
    assert result.counts == {"email": 1}


def test_overlapping_matches_keep_the_longest():
    # a card-shaped run also contains national-id and mobile-shaped substrings
    result = redact_identifiers("Kort 4111 1111 1111 1111", kinds=("national_id", "card"))
    assert result.counts.get("card") == 1
    assert result.text.count("[") == 1


def test_counts_match_placeholders():
    text = "Tlf: 98251606 og 97545490, post 5238 Rådal"
    result = redact_identifiers(text)
    for kind, count in result.counts.items():
        assert result.text.count(f"[{kind}]") == count


def test_redaction_is_idempotent():
    once = redact_identifiers("Tlf: 98251606")
    twice = redact_identifiers(once.text)
    assert once.text == twice.text
    assert twice.total == 0


# ── labelled identifiers ─────────────────────────────────────


def test_redacts_a_labelled_member_number():
    """The measured case: a union newsletter carrying a membership number."""
    text = "Nyhetsbrev tillitsvalgt\nMedlemsnummer: 51764694"
    result = redact_identifiers(text)
    assert result.counts.get("labelled_identifier") == 1
    assert "51764694" not in result.text


def test_labelled_identifier_keeps_the_label():
    text = "Medlemsnummer: 51764694"
    out = redact_identifiers(text).text
    assert "Medlemsnummer" in out, "the field name must survive for the entry to make sense"
    assert out.endswith("[labelled_identifier]")


def test_labelled_identifier_accepts_common_shapes():
    for text in ("Medlemsnr 51764694", "Kundenr: 123456", "Kunde-ID 9080706",
                 "Kundenummer #4455667", "medlemskapsnummer 7654321"):
        assert redact_identifiers(text).counts.get("labelled_identifier") == 1, text


def test_labelled_identifier_ignores_transaction_numbers():
    """An order or case number identifies a transaction, not a person."""
    for text in ("Ordrenummer: 51764694", "Saksnummer 20241158", "Fakturanr 99887766"):
        assert redact_identifiers(text).total == 0, text


def test_labelled_identifier_needs_digits():
    assert redact_identifiers("Medlemsnummer: ikke oppgitt").total == 0
    assert redact_identifiers("Medlemsnummer: 12").total == 0


def test_transaction_guard_does_not_suppress_a_real_phone():
    """The guard must only fire on transaction labels."""
    for text in ("Tlf: 99887766", "Ring 99887766", "Telefon 99887766"):
        assert redact_identifiers(text).counts.get("mobile_bare") == 1, text


def test_labelled_identifier_is_in_the_default_set():
    from threadweave.text_hygiene import DEFAULT_KINDS

    assert "labelled_identifier" in DEFAULT_KINDS


# ── identifier-only detection ────────────────────────────────


def test_identifier_only_recognises_a_roster():
    text = "\n".join(f"{i:06d}{i:05d}" for i in range(1, 12))
    assert is_identifier_only(text)


def test_identifier_only_is_false_for_real_content():
    text = f"{BODY}\n\nTlf: 98251606"
    assert not is_identifier_only(text)


# ── the combined helper ──────────────────────────────────────


def test_sanitise_strips_then_redacts():
    text = f"{BODY}\n\nMvh\nHarald\nTlf: 98251606\nharald@example.no"
    stored, redaction, strip = sanitise(text)
    assert isinstance(redaction, Redaction) and isinstance(strip, SignatureStrip)
    assert strip.stripped
    assert "98251606" not in stored
    assert "arkitekturnotatet" in stored


def test_sanitise_leaves_work_contact_details_in_the_body_alone():
    text = f"{BODY}\n\nKontaktperson for saken er post@example.com."
    stored, redaction, _ = sanitise(text)
    assert "post@example.com" in stored
    assert redaction.total == 0
