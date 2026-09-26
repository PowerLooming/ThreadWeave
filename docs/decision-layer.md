# Typed decision layer

ThreadWeave's ingest gate asks five narrow questions about every message: is
this worth keeping, is it gossip, does it carry personal data, what language is
it in, how wide is its scope. Those are judgments, not prose, so they are asked
as typed questions instead of being fished back out of a model's JSON reply.

The layer is off by default. With `THREADWEAVE_DECISION_PROVIDER` unset, the
pipeline uses the LLM detector and the regex classifier exactly as before.

## Primitives

Three question types, mirroring the TypeSafe AI "System One" contract so a
hosted decision model can be dropped in behind the same interface:

| Type | Question | Answer |
|---|---|---|
| `Noul` | Is this statement true? | probability 0.0 to 1.0 |
| `Choice` | Which of these options? | option, probabilities, confidence |
| `Score` | Which level on this rubric? | score, probabilities, confidence |

All questions for one message ride in a single provider call. Confidence is
derived from the probability distribution with the same formula TypeSafe
documents, `(n * peak - 1) / (n - 1)`, so thresholds mean the same thing no
matter which provider answered.

## Providers

| Provider | Where it runs | Notes |
|---|---|---|
| `encoder` | local, on-prem | a small NLI classifier (`transformers` + `torch`). Returns real distributions computed from logits, needs no GPU, and asks one question per call. Default model `MoritzLaurer/bge-m3-zeroshot-v2.0-c` (MIT, multilingual, covers Norwegian). |
| `ollama` | local, on-prem | one batched call to `/api/chat` with a JSON schema pinned as the response format. Fastest on a GPU, but its probabilities are self-reported by a generative model, so its confidences are uncalibrated by construction. |
| `typesafe` | hosted (api.typesafe.ai) | the seam for Jev. Refuses to run unless `THREADWEAVE_DECISION_ALLOW_REMOTE` is truthy, because content does not leave the machine it was captured on. |

```bash
# encoder (no GPU needed)
THREADWEAVE_DECISION_PROVIDER=encoder
THREADWEAVE_DECISION_MODEL=MoritzLaurer/bge-m3-zeroshot-v2.0-c
uv pip install -e ".[decisions]"

# or ollama
THREADWEAVE_DECISION_PROVIDER=ollama
THREADWEAVE_DECISION_MODEL=qwen3.5:9b
THREADWEAVE_LLM_BASE_URL=http://localhost:11434
```

Use the `-c` model variants. The card states those are trained only on
commercially-friendly data, while the non `-c` checkpoints include data under
non-commercial licences. The English-only checkpoints (`ModernBERT-base-zeroshot-v2.0`,
`deberta-v3-large-zeroshot-v2.0-c`) are faster but do not classify Norwegian.

For a second opinion on top of the typed answers, the escalation engine is the
existing LLM detector. It needs its own provider hint, otherwise it posts to the
OpenAI path and silently degrades to regex:

```bash
THREADWEAVE_LLM_PROVIDER=ollama
THREADWEAVE_LLM_MODEL=qwen3.5:9b
```

## Phrasing rules for the encoder

Entailment models are sensitive to how a question is written, and this is the
difference between a working gate and a useless one, measured on the same
samples:

* Option and level text is wrapped in a template (`This message is {}.`), so a
  Choice is asked about descriptions, not about bare ids.
* `Noul` questions carry a short single-clause `statement`. Long multi-clause
  statements collapse: the gossip statement scored 0.032 and the PII statement
  0.527, while the short forms scored 0.865 and 0.846 on the same messages.
* The language question is not asked at all: a Choice over language names picked
  the right language only 0.17 to 0.23 of the time. The provider reports
  `answers_language = False` and `language_id.py` answers instead, which is
  deterministic and needs no model.

## Policy

Thresholds live in code, never in a prompt, and each one is a risk decision:

| Env var | Default | Meaning |
|---|---|---|
| `THREADWEAVE_DECISION_MIN_CONFIDENCE` | 0.55 | below this the typed answer is not trusted and the run escalates |
| `THREADWEAVE_DECISION_GOSSIP_REJECT_AT` | 0.80 | rejection destroys knowledge, so it needs a high bar |
| `THREADWEAVE_DECISION_GOSSIP_REVIEW_AT` | 0.50 | between review and reject the message is flagged and a second opinion is asked for |
| `THREADWEAVE_DECISION_PII_REJECT_AT` | 0.75 | PII false positives reject ingest, so this stays conservative |
| `THREADWEAVE_DECISION_LANGUAGE_MIN_CONFIDENCE` | 0.40 | a wrong language guess costs a re-translation, not data |
| `THREADWEAVE_DECISION_LANGUAGE_ID_MIN_CONFIDENCE` | 0.25 | the deterministic detector reports a margin, not a distribution, so it gets its own scale |

Thresholds are not transferable between providers, and none of them are
calibrated yet. Fit temperature scaling or isotonic regression per question type
on a labelled sample from your own corpus before treating the numbers as
meaningful.

Generative work (titles, entities) is deliberately not asked as a decision. It
comes from the escalation engine, or from the regex classifier when no LLM is
configured.

## Verifying it is active

`GET /api/v1/health` reports which engine is classifying and the thresholds in
force, so a serve that quietly fell back to regex is visible:

```json
{
  "detector": "decisions:ollama",
  "decision_provider": "ollama",
  "decision_policy": {"min_confidence": 0.55, "gossip_reject_at": 0.80}
}
```

Every decision is appended to `~/.threadweave/decision_audit.jsonl` (override
with `THREADWEAVE_DECISION_AUDIT`) with the content hash, a short excerpt, the
per-question answer and its confidence. That file is how a rejected message is
explained after the fact.

## Measured on one machine (2026-09-26)

`bge-m3-zeroshot-v2.0-c`, four questions per message, RTX 3060 Ti (8 GB):

| Sample | Result |
|---|---|
| English decision | `decision@0.70`, language en (detector 0.71) |
| Norwegian decision | `decision@0.68`, language no (detector 0.26) |
| Gossip about a colleague | `is_gossip=0.865`, rejected at the 0.80 bar |
| Work criticism | `is_gossip=0.077`, kept |
| Personal ID numbers | `has_pii=0.846`, rejected at the 0.75 bar |
| Newsletter | `reference@0.33`, below the confidence floor so it escalates |
| "ok thanks" | `too_short`, no model call |

Cost, same samples, same model:

| Build | Per message | First message |
|---|---|---|
| CPU-only torch | 8 to 10 seconds | 19 seconds |
| CUDA torch (`+cu130`, installed) | 0.24 to 0.28 seconds | 24 seconds (weights load plus CUDA warmup) |
| ollama on the GPU (for comparison) | about 5 seconds | 16 seconds |

The answers are identical between the CPU and CUDA builds, to the third decimal.
Install the CUDA build with:

```bash
uv pip install --reinstall --index-url https://download.pytorch.org/whl/cu130 torch
```

Check which CUDA build the driver supports, then use that index (`cu126`, `cu130`,
and so on; not every torch release publishes every index). The model occupies
roughly 2.3 GB of VRAM in fp32 and coexists with an ollama model on the same card.
The encoder's content-type confidence is lower than the ollama provider's
(0.68 to 0.70 against 1.00), so more messages reach the escalation engine.
