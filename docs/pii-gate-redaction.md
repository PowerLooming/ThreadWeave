# PII gate: redact instead of delete, and do not let signatures decide

Status: shipped (merged), live in 0.4.17.
Mechanisms in `src/threadweave/text_hygiene.py`, wired into the ingest endpoint in
`src/threadweave/api.py`, tests in `tests/test_text_hygiene.py` and
`tests/test_api_pii_redaction.py`. Evidence below is measured; see the last two sections.

## What is implemented

- `strip_signature(text)` — two tiers. A marker in the last 40% of the message is honoured
  when the block after it looks like a signature (a contact feature, or under 400
  characters). A marker further up needs an unambiguous block: at least two contact or
  company features. At least 100 characters of body always remain, so the stripper can
  never push a message under the email processor's own floor. Returns what it did.
- `redact_identifiers(text, kinds=...)` — replaces national IDs, bank accounts, card
  numbers, Norwegian mobiles with and without the country code, street addresses, postal
  code and place, labelled person identifiers (`Medlemsnummer: 10000001`, `Kundenr 123456`)
  and (when asked) email addresses with typed placeholders. Decodes HTML entities first and
  masks URLs, hashes and base64 runs before matching. Shape patterns skip a number that
  follows a transaction label (`Ordrenummer`, `Fakturanr`, `Sak`, `request number`, `build
  number`), because a reference is not a person's identifier: `Fakturanr 99887766` is eight
  digits starting with 9 and otherwise reads as a mobile, and a Microsoft support ticket
  number is sixteen digits and otherwise reads as a card.
- `is_identifier_only(text)` — structural test for content whose substance is the
  identifiers, a pasted roster or ID list, the one case where redaction leaves nothing.
- Ingest wiring: the detector now sees the signature-stripped text; a PII verdict redacts
  and keeps the message and reports `redacted: {kind: count}` on the response;
  `THREADWEAVE_PII_MODE=reject` restores the old destructive verdict; identifier-only
  content is still rejected; `ingest_redacted_pii` joins the metrics.

## The problem

The decision gate treats `has_pii` as a destructive verdict: `detector.py` states it in a
comment, "has_pii=True REJECTS the ingest", and `_apply_policy` in `decision_gate.py` only
sets a flag that downstream code turns into a dropped message. There is no redaction path
anywhere in the repository, and no signature handling either.

That is survivable only while PII means "a national ID or a card number". It stops being
survivable the moment the trigger includes a phone number, which the PII question naturally
suggests and which the labels for a real mailbox confirm. A Norwegian mobile and a Norwegian
work mobile are the same shape: eight digits starting with 4 or 9. A pattern can therefore
only ever say "a phone number is present". In a work mailbox that means "a signature is
present", because signatures carry switchboard and mobile numbers by design, and the gate
would delete most of the mail it is supposed to remember.

Measured on 1,692 real messages from a personal mailbox (a work mailbox is worse, not
better, because signatures are the norm there rather than the exception):

| measurement | value |
|---|---|
| messages with a signature marker | 18.1% |
| messages carrying a Norwegian mobile | 7.7% |
| messages whose signature or disclaimer is stripped by the two-tier rules | 416 (24.6%) |
| of those, stripped from outside the default window by the strict rule | 3 |
| messages a credential pattern would delete today | 116 (6.9%) |

An earlier draft of this document quoted 5.7% for the strip rate. That was a counting bug
in the prototype rather than a different rule: it counted signature blocks only and filed
disclaimer strips under a different bucket, so the true figure was always about a quarter.
The guards are what make the higher number safe, and they are tested: a signature word
inside a sentence is not a marker, a block that does not look like a signature is left
alone, and a strip that would leave under 100 characters is refused.

Deleting 6.9% of a mailbox to protect it is a bad trade, and it gets worse in exactly the
deployment this system exists for.

## The second problem: patterns do not survive contact with real mail

The first prototype ran naive identifier patterns over the same 1,692 messages and reported
348 national-ID-shaped hits and 25 card-shaped hits. Sampling them showed every one was a
digit run inside a tracking URL or a marketing parameter: `?qs=`, `elqTrackId=`, Facebook
post IDs. "Postal code plus place" was matching years and times of day.

Two consequences, both load-bearing for the design:

- Redaction built on unmasked patterns silently corrupts stored knowledge, replacing URL
  fragments with `[national_id]` and hashes with `[card]`.
- Any deletion policy built on them destroys mail for no reason.

With URL and hex/hash masking plus entity decoding, the same corpus reports 13 national-ID
hits, zero card hits, 54 bare mobiles, 46 street addresses, 30 country-code mobiles, one
bank account, and the blast radius above falls from 13.1% to 6.4%.

## What must never be stored, and what must always be shared

The gate needs to distinguish two things it currently treats as one:

- Identifiers best not stored at all, or stored with the identifier replaced: national ID,
  card and bank account numbers, private phone numbers, home addresses, individual salaries,
  health information.
- Contact details that exist to be shared: work email addresses, work phone numbers, office
  addresses, organisation numbers, roles and titles. A work signature is publishing
  information; deleting the message because it contains one is a bug.

The first group is pattern-shaped. The second group is not, which is why "any phone number"
cannot be the rule and why the phone decision needs either context or the tier-2 judge.

## Design

Four mechanisms, in the order they run.

1. Normalisation before detection. Decode HTML entities (`&#43;` hides a `+47` from every
   pattern today) and mask URL, hex and base64 spans so identifier patterns cannot match
   inside them. Masking is for matching only: the stored text keeps its URLs unless a real
   identifier is inside one.

2. Signature and disclaimer stripping. Cut a trailing block only when a signature marker or
   disclaimer line appears in the last 40% of the message and what follows looks like a
   signature (contains an email, phone, URL or company token, or is at most 12 lines), and
   only when at least 100 characters of body remain. Measured behaviour of exactly these
   rules: 96 of 1,692 messages stripped (5.7%), 427 markers declined because the tail did not
   look like a signature, 1,068 without markers untouched. Conservative on purpose: dropping
   body text is worse than keeping a signature.

3. Redaction instead of rejection. A PII verdict replaces the identifier with a typed
   placeholder (`[national_id]`, `[mobile_no]`, `[postal_address]`, `[email]`, ...) and the
   message is stored. The entry carries a redaction record: kinds and counts, never the
   values. The decision audit records the same, so a noisy class can be found and tuned
   later, and so an operator can ask "what did we strip and why".

4. Deletion stays available, but only for identifier-only content: a message whose substance
   is the identifiers, such as a pasted roster or an ID list, where redaction would leave
   nothing worth keeping. The criterion is structural (ratio of identifier characters to
   total, no other content), not a threshold on the model's confidence.

Configuration: `THREADWEAVE_PII_MODE` with `redact` as the default and `reject` retained for
deployments that want the old behaviour, plus `THREADWEAVE_PII_REDACT_KINDS` to select the
kinds. The reject bar keeps its meaning for the identifier-only case; it no longer decides
whether a normal message is lost.

## Pattern evidence is fused into the verdict

Redaction is gated on `result.has_pii`, and under a decision provider that flag is the model's
opinion against `pii_reject_at` (0.75). Measured live on 2026-10-02 (version 0.4.17, provider
`encoder`): a message carrying a Norwegian mobile, a `Kundenr` and a card number scored
`pii=0.06`, came back `has_pii: false, redacted: null`, and was stored with all three
identifiers intact. The patterns that were built for exactly that case were never consulted,
because `detector.PII_PATTERNS` runs only on the regex-fallback path, which the gate
pre-empts.

`detector.fuse_pattern_pii` ORs the identifier scan into every engine's verdict, and both
`detect_async` and `is_worth_saving_async` apply it, so the synchronous path, the modelled
paths and `/api/v1/detect` all agree on what counts as PII. The scan reuses the redactor's own
patterns and the kind set from `text_hygiene.configured_kinds`, which the redactor also reads:
a verdict that fires on something the redactor cannot replace would report a redaction that
never happened, and in `reject` mode a verdict broader than the configured kinds would delete
mail for a kind the deployment had deselected.

Fusion does not touch `should_save`. Worth-saving is a question about knowledge, the PII
verdict is a question about safety, and one must not decide the other.

The semantic half stays with the model, as described below: a name with relationship framing
has no pattern to match, and the fusion is honest about that rather than pretending the
pattern layer covers it.

## What this deliberately does not solve

The semantic half of PII. A message can carry personal data about private individuals with
no identifier in it at all: names with family or relationship framing, genealogy hints, who
is related to whom. This was measured on a real mailbox: the NLI encoder scored 0.002 to
0.106 on five such messages, while an LLM scored 0.90 to 1.00. That half belongs to the
tier-2 judge, which is a separate change, and redacting it is harder than redacting a phone
number because the personal data is the sentence.

Until the tier-2 work lands, the honest description of the PII gate is: patterns plus
redaction for identifiers, and a model's opinion for the semantic half, with the audit
record showing which of the two fired.

## Tests this change needs

- Stripper: a message with a signature and real content keeps the content and loses the
  block; a message with "Hilsen" mid-body keeps everything; a short message with a marker
  and no body is left untouched; a disclaimer block is removed; a message with neither is
  unchanged byte for byte.
- Redactor: entity-encoded `&#43;47` is caught; a phone inside a `?qs=` URL is not; a
  hash-looking run is not; overlapping matches keep the longest; the counts match the
  placeholders.
- Gate: a PII hit stores the message with the identifier replaced and adds the redaction
  record to `DecisionResult` and the audit; no path deletes an ordinary message.
- Pipeline: a message with a signature, a mobile and engineering content ends up stored,
  searchable, and without the number.

## Reproducing the measurements

Prototype, deliberately kept outside the public repo, alongside the calibration corpora and
labelled sets used for the wider comparison: `tw_signature_strip.py` (`--measure` for the
table above, `--stripped DIR` to write stripped bodies).
