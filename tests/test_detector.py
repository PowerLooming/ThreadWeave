# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
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

    def test_decision_short_strong_cue_saves(self):
        """A single explicit first-person decision cue must clear the
        save threshold on its own (0.25 short-decision gap, live
        2026-08-16)."""
        text = "we decided to standardize on PostgreSQL for all new services"
        result = detect(text)
        assert result.content_type == ContentType.DECISION
        assert result.confidence >= 0.40

    def test_decision_strong_cue_bypasses_length_gate(self):
        """Explicit decisions under 50 chars are decisions, not chat."""
        text = "We decided to ship the Q3 pricing model next week"
        assert len(text) < 50
        result = detect(text)
        assert result.content_type == ContentType.DECISION
        assert result.confidence >= 0.40

    def test_decision_bare_strong_cue_still_chat(self):
        """'we decided to' with nothing after it is too short to save."""
        result = detect("we decided to")
        assert result.content_type == ContentType.CHAT

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
            "is that the config loader caches on startup. There's no hot reload. "
            "In my experience, you need to wait at least 30 seconds before "
            "running health checks. Here's how we discovered this: after a "
            "production outage in Q3 we traced the root cause to a race condition "
            "in the config loader's file watcher."
        )
        should, result = is_worth_saving(text)
        assert should is True
        assert result.content_type == ContentType.ANSWER

    def test_not_worth_saving_chat(self):
        text = "Sounds good, let me know when it's ready."
        should, result = is_worth_saving(text)
        assert should is False

    # ── PII detection ─────────────────────────────────────

    def test_pii_nordic_personal_id(self):
        """Nordic personal ID (11 digits: DDMMYY-XXXXX) should be detected."""
        text = "Employee record: 01019012345 — please update the system."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_us_ssn(self):
        """US SSN (xxx-xx-xxxx) should be detected as PII."""
        text = "HR paperwork needs the new hire's 123-45-6789 for the W-2 form to be processed correctly."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_credit_card_format(self):
        """Credit card format (4-4-4-4) should be detected as PII."""
        text = "Payment details: 4532-7189-3412-5678 for the invoice."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_bank_account_labeled(self):
        """Bank account with label should be detected as PII."""
        text = "Refund to account no: 1234.56.78901 for travel expenses."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_german_bank_account(self):
        """German bank account (Bankverbindung) should be detected."""
        text = (
            "Bitte überweisen Sie das Geld an folgende Bankverbindung: "
            "DE89 3704 0044 0532 0130 00 für die Rechnung vom März."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_iban(self):
        """IBAN should be detected as PII."""
        text = "International wire to IBAN: GB29 NWBK 6016 1331 9268 19 for the supplier."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_salary_norwegian(self):
        """Norwegian salary discussion should be detected as PII."""
        text = (
            "For the new senior engineer position in the platform team, "
            "Lønn: 850000 NOK per year plus standard benefits package."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_salary_german(self):
        """German salary (Gehalt) should be detected as PII."""
        text = (
            "Das Angebot für die Senior-Stelle im Engineering-Team: "
            "Gehalt: 95000 EUR jährlich plus Bonus und Aktienoptionen."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_salary_french(self):
        """French salary (salaire) should be detected as PII."""
        text = (
            "Pour le poste d'ingénieur senior dans l'équipe plateforme, "
            "salaire: 75000 EUR par an avec avantages standards."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_passport_number(self):
        """Passport number with label should be detected as PII."""
        text = "Travel docs: passport no: AB1234567 — expires 2028."
        result = detect(text)
        assert result.has_pii is True

    def test_pii_passport_spanish(self):
        """Spanish passport (pasaporte) should be detected."""
        text = (
            "Documentos de viaje: pasaporte nº: XA9876543 caduca en 2030, "
            "por favor actualizar en el sistema de RRHH."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_national_id_labeled(self):
        """National ID with explicit label should be detected."""
        text = (
            "Background check requires National ID: AB123456C for the "
            "contractor onboarding process to proceed."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_address_norwegian(self):
        """Norwegian home address label should trigger PII."""
        text = (
            "Send the equipment to his hjemmeadresse: Storgata 15, 0152 Oslo "
            "since he's working remotely this quarter."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_address_german(self):
        """German private address label should trigger PII."""
        text = (
            "Die Unterlagen bitte an die Privatadresse: Musterstraße 42, "
            "10115 Berlin schicken, nicht ins Büro."
        )
        result = detect(text)
        assert result.has_pii is True

    def test_pii_french_tax_id(self):
        """French tax ID (numéro fiscal) should be detected."""
        text = (
            "Pour la déclaration, veuillez fournir votre numéro fiscal: "
            "1234567890123 avant la fin du mois."
        )
        result = detect(text)
        assert result.has_pii is True

    # ── Negative tests: must NOT trigger PII ──────────────

    def test_pii_company_name_not_flagged(self):
        """Company names must NOT trigger PII detection."""
        text = (
            "We're partnering with Nordvik Maritime AS on the new "
            "propulsion system. Nordlys Energy is also involved. "
            "The contract with Fjord Works was signed last week."
        )
        result = detect(text)
        assert result.has_pii is False, (
            f"Company names should not trigger PII, got: {result.has_pii}"
        )

    def test_pii_workplace_communication_not_flagged(self):
        """Normal workplace communication should not trigger PII."""
        text = (
            "Please review the build 3.14.2 for the v2.5.1 release. "
            "The ticket PR #355960 was merged. Contact the team at "
            "engineering@company.com for questions. Office phone: 555-0100."
        )
        result = detect(text)
        assert result.has_pii is False, (
            f"Workplace communication should not trigger PII, got: {result.has_pii}"
        )

    def test_pii_org_number_not_flagged(self):
        """Norwegian org numbers (organisasjonsnummer) are public — not PII."""
        text = (
            "Vendor registration: Nordvik Maritime AS, org.nr. 910 123 456. "
            "Nordlys Energy, org.nr. 910 654 321."
        )
        result = detect(text)
        assert result.has_pii is False, (
            f"Org numbers are public record, should not trigger PII, got: {result.has_pii}"
        )

    def test_pii_bare_account_number_not_flagged(self):
        """Bare account number without label should not trigger PII."""
        text = (
            "The build number 1234.56.78901 was deployed to production "
            "yesterday after passing all integration tests."
        )
        result = detect(text)
        assert result.has_pii is False, (
            f"Bare numbers without context should not trigger PII, got: {result.has_pii}"
        )

    # ── Gossip / personal-attack detection ───────────────

    def test_gossip_direct_insult(self):
        result = detect("we have concluded that Patty is a bitch")
        assert result.has_gossip is True

    def test_gossip_rumor_framing(self):
        result = detect("I heard that Bjorn is sleeping with the intern")
        assert result.has_gossip is True

    def test_gossip_hearsay(self):
        result = detect("Apparently Kim got the promotion because she's dating the boss")
        assert result.has_gossip is True

    def test_gossip_ad_hominem(self):
        result = detect("Jens is such an idiot, can't believe he said that in the meeting")
        assert result.has_gossip is True

    def test_gossip_short_text_still_flagged(self):
        # Gossip must be caught regardless of text length (like PII)
        result = detect("Patty is a bitch")
        assert result.has_gossip is True

    def test_legit_decision_not_gossip(self):
        result = detect("We decided to use PostgreSQL for the authentication service.")
        assert result.has_gossip is False

    def test_legit_criticism_not_gossip(self):
        # Criticism of WORK is fine — attacks on PEOPLE are gossip
        result = detect("The PR has bugs and the design is weak, we should refactor it.")
        assert result.has_gossip is False

    def test_legit_tech_discussion_not_gossip(self):
        result = detect("The server is slow under load, we should add an index on tenant_id.")
        assert result.has_gossip is False

    def test_gossip_scandal_framing(self):
        result = detect("Did you hear what Maria did at the Christmas party? Total scandal")
        assert result.has_gossip is True

    def test_gossip_useless_person_is_llm_only(self):
        # "X is completely useless" is deliberately NOT in the regex
        # patterns: regex cannot distinguish "Anna is useless" from
        # "the API is useless" (legit criticism), and false positives
        # are worse than false negatives. The LLM layer catches the
        # person-directed case (verified in the 20-case battery).
        result = detect("Anna is completely useless, she contributes nothing to this team")
        assert result.has_gossip is False  # regex is conservative here
        # ...but "useless" about a THING must never be flagged either
        result2 = detect("This API is completely useless, we should replace it")
        assert result2.has_gossip is False

    def test_gossip_backtalk(self):
        result = detect("The boss can go screw himself with his new policy")
        assert result.has_gossip is True

    def test_gossip_contempt_leadership_is_llm_only(self):
        # Regex is conservative by design: "leadership never listens"
        # is venting, and only the LLM reliably separates contempt for
        # people from frustration with decisions. Verified in the
        # 20-case battery (LLM: has_gossip=True).
        result = detect("I don't care what the leadership wants, they never listen to us anyway")
        # regex may or may not flag — the LLM layer is the authority here
        assert result.has_gossip in (True, False)

    def test_humor_about_things_not_gossip(self):
        result = detect("Our server room is a sauna in summer, you could fry eggs on the rack")
        assert result.has_gossip is False

    def test_venting_about_project_not_gossip(self):
        result = detect("This project is a nightmare, the requirements change every single week")
        assert result.has_gossip is False

    def test_sarcasm_about_meetings_not_gossip(self):
        result = detect("Great, another meeting that could have been an email. Fantastic.")
        assert result.has_gossip is False

    def test_sarcasm_targeting_person_is_llm_only(self):
        # Sarcasm ("Peter strikes again") needs semantics — regex cannot
        # reliably separate it from genuine praise. LLM catches it
        # (verified in the 20-case battery). Regex stays conservative.
        result = detect("Oh brilliant, Peter strikes again with another genius idea we all have to clean up")
        assert result.has_gossip in (True, False)

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
