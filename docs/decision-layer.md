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
| `ollama` | local, on-prem | default choice. One batched call to `/api/chat` with a JSON schema pinned as the response format. |
| `typesafe` | hosted (api.typesafe.ai) | the seam for Jev. Refuses to run unless `THREADWEAVE_DECISION_ALLOW_REMOTE` is truthy, because content does not leave the machine it was captured on. |

```bash
THREADWEAVE_DECISION_PROVIDER=ollama
THREADWEAVE_DECISION_MODEL=qwen3.5:9b
THREADWEAVE_LLM_BASE_URL=http://localhost:11434
```

For a second opinion on top of the typed answers, the escalation engine is the
existing LLM detector. It needs its own provider hint, otherwise it posts to the
OpenAI path and silently degrades to regex:

```bash
THREADWEAVE_LLM_PROVIDER=ollama
THREADWEAVE_LLM_MODEL=qwen3.5:9b
```

## Policy

Thresholds live in code, never in a prompt, and each one is a risk decision:

| Env var | Default | Meaning |
|---|---|---|
| `THREADWEAVE_DECISION_MIN_CONFIDENCE` | 0.55 | below this the typed answer is not trusted and the run escalates |
| `THREADWEAVE_DECISION_GOSSIP_REJECT_AT` | 0.80 | rejection destroys knowledge, so it needs a high bar |
| `THREADWEAVE_DECISION_GOSSIP_REVIEW_AT` | 0.50 | between review and reject the message is flagged and a second opinion is asked for |
| `THREADWEAVE_DECISION_PII_REJECT_AT` | 0.75 | PII false positives reject ingest, so this stays conservative |
| `THREADWEAVE_DECISION_LANGUAGE_MIN_CONFIDENCE` | 0.40 | a wrong language guess costs a re-translation, not data |

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
