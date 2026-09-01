# ADR 0034 — Confidence is computed by the platform, and the model may only lower it

**Status:** Accepted · 2026-09-01

## Context

`AgentOutput.confidence` is a float the model emits. Until now it was stored on
`agent_tasks.agent_confidence` verbatim, and both the model docstring and the API
said so plainly: "confidence is consulted by nothing", "displayed and consulted by
nothing". That was accurate, and while the number stayed internal it was also
harmless.

MerchantOps v2 §63, §64 and §102 render it. The incident detail screen the
specification draws reads:

```text
ROOT CAUSE
UPI payment degradation is the primary observed driver.

Evidence: 4 independent signals.
Confidence: HIGH
```

The moment that screen exists, `HIGH` is a claim by the platform to a merchant
who is about to decide whether to authorise a recovery campaign. Its only
support was that the model said so.

v2 §33 addresses this directly — "LLM confidence should not be blindly trusted" —
and lists the six inputs a platform-owned model should weigh instead: evidence
quality, evidence agreement, data freshness, historical consistency, provider
confirmation, and the number of independent signals.

There is also a security argument, which is the one that settles it. §39 already
establishes that customer and order free text is untrusted data. If self-reported
confidence set the band, and the model's confidence is influenced by the text it
read, then someone who can write an order note has an influence on the number a
merchant reads as the system's certainty. The existing injection defences stop
that text from *executing* anything. They do not stop it from being persuasive.

## Decision

1. `app/agent/confidence.py` computes a band — `HIGH`, `MEDIUM`, `LOW`,
   `INSUFFICIENT` — from the incident's evidence. It is stored on
   `incidents.confidence_band`, with the full derivation in
   `confidence_inputs`.
2. `agent_tasks.agent_confidence` is unchanged in meaning and still stored. It is
   now an **input** to the band, with a strict asymmetry:

   > The model may lower the band. It may never raise it.

   This mirrors `requires_human` in `app/agent/output.py`, which may only raise
   the bar. The reasoning is the same: a model that volunteers doubt has told us
   something we could not otherwise observe; a model that asserts its own
   reliability has made the one claim it has no standing to make.
3. Untrusted evidence is counted and never corroborates.
4. `INSUFFICIENT` is distinct from `LOW`. "Not enough evidence to have a view"
   and "evidence that does not support the finding" lead to different next
   actions, exactly as `FAILED` and `UNKNOWN` do in §53.
5. Provider confirmation — evidence whose source is `razorpay` or
   `webhook_events` — can lift `MEDIUM` to `HIGH` and deliberately cannot rescue
   `LOW`. Confirming one fact externally does not make a thin case a broad one.
   It is derived from the evidence rather than passed in: a parameter a caller
   has to remember is a parameter that eventually gets passed `True` by habit,
   and this is the one input that can raise a band.

## The bands

| Band | Meaning |
|---|---|
| `HIGH` | ≥3 independent, fresh, trusted sources agree; no failed tool calls; or 2 sources plus external provider confirmation |
| `MEDIUM` | ≥2 independent sources, but thin, partly stale, or with a tool call that failed |
| `LOW` | a single source, or evidence that is entirely stale |
| `INSUFFICIENT` | no trusted evidence at all |

"Independent" means a distinct `source` on the evidence row. Five readings from
`payments` are one signal read five times, which is v2 §18's argument applied to
a single incident's case rather than to detection.

## Consequences

- **Displayed confidence will drop for most incidents, and that is the
  correction.** A payment-degradation incident draws its evidence from `payments`
  and `calculation_engine` — two sources — so it now reads `MEDIUM` where a model
  emitting `0.9` previously implied high certainty. An incident reaches `HIGH`
  when it has genuinely broad support, which is the point.
- The band is null before investigation. An incident nobody has assessed has no
  confidence, and defaulting it to a value would assert one.
- `confidence_inputs` is stored because a band with no derivation is the opaque
  number this replaces. "Why HIGH?" has an answer.
- The asymmetry needs a test that a future refactor cannot quietly drop, since
  the failure is silent: a band that got easier to reach still returns a valid
  value. `test_a_confident_model_cannot_raise_the_band` is that test.
- Historical consistency is the one input from v2 §33's six that is **not**
  implemented. It needs a record of how often this rule's past findings held up,
  which nothing produces yet. Recorded here rather than approximated: a factor
  computed from nothing would be worse than its absence, on the same reasoning
  `app/metrics.py` uses when it reports a metric as unavailable rather than
  inventing one.
