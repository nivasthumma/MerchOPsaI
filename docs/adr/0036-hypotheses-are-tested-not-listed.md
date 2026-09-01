# ADR 0036 — Hypotheses are tested, and the platform adjudicates

**Status:** Accepted · 2026-09-02

## Context

MerchantOps v2 §30 asks for competing hypotheses:

```text
H1  UPI provider degradation
H2  Merchant configuration problem
H3  Traffic anomaly
H4  Customer-segment-specific problem
```

and then, crucially, for evidence to be gathered against each, ending with
"H1 = strongest supported explanation". §30 closes with the reason: "This is
more robust than a single-shot LLM response."

The temptation with this section is to write four sentences into a table and
call it a hypothesis engine. That would be worse than not building it. A list of
candidate explanations that cannot be *contradicted* adds ceremony to a guess
and, because it looks like reasoning, makes the guess harder to question.

The build already had the pieces this needs: ADR-0034's rule that the platform
owns judgements the model would otherwise assert about itself, and ADR-0035's
`CONTRADICTS` predicate, added to the evidence graph for exactly this.

## Decision

1. `app/evidence/hypotheses.py` holds a template set per incident type and a
   **probe** per hypothesis. Each probe runs a real query and returns a verdict
   that could have gone the other way.
2. **The platform adjudicates.** Hypotheses may be proposed by the template set
   or added by the model; which one survives is computed by `adjudicate` from
   supporting and contradicting evidence. This is ADR-0034's rule applied to
   explanations rather than to confidence.
3. Every verdict is **drawn into the evidence graph** as a `SUPPORTED_BY` or
   `CONTRADICTS` edge with the probe's facts attached, so a rejection is
   walkable rather than stated.
4. `hypothesis.created` and `hypothesis.rejected` are published. They were two
   of v2 §62's fifteen frames with no producer.

## The probes, and what each can actually decide

| hypothesis | probe | verdict on the seeded data |
|---|---|---|
| `provider_degradation` | share of failures carrying one error code; provider-reported burst | **SUPPORTED** — 100% carry `UPI_COLLECT_TIMEOUT` |
| `traffic_anomaly` | attempt volume during vs. before the incident window | **REJECTED** — volume moved −14% |
| `customer_segment` | failures per distinct affected customer | **REJECTED** — 43 failures across 37 customers |
| `merchant_configuration` | — | **UNTESTED** — nothing here records it |

`traffic_anomaly` is the one worth dwelling on. It is the hypothesis a reader
assumes away, and rejecting it on measured volume is what makes the surviving
explanation mean something. `test_a_probe_changes_its_mind_when_the_data_changes`
inserts a genuine volume spike and watches the same probe support it instead —
without that test, the verdicts in the table are constants wearing a query.

## Four statuses, not two

```text
SUPPORTED    the sole survivor
CONTENDING   supported, but so is something else
REJECTED     evidence positively argues against it
UNTESTED     nothing here can speak to it either way
```

**CONTENDING** exists because promoting the first of two surviving explanations
is precisely the single-shot answer §30 was written against. When traffic and
provider degradation both fit, neither is named the cause and `leading` returns
null.

**UNTESTED** exists for the same reason §53 keeps `UNKNOWN` apart from `FAILED`
and ADR-0034 keeps `INSUFFICIENT` apart from `LOW`. "We cannot test this" and
"we tested this and it failed" lead to different next actions: the first is a
gap in instrumentation, and reporting it as a rejection hides that gap behind a
verdict that looks settled. `merchant_configuration` is genuinely untestable
here — this system stores no merchant payment configuration or change history —
and the API names it in `untested` rather than leaving it to be counted off a
list.

## What is deliberately not built

- **No hypothesis for every incident type.** A duplicate payment has one
  explanation; manufacturing three more to reject would be ceremony.
  `candidates_for` returns empty and `adjudicate` returns nothing.
- **No model-proposed hypotheses yet.** The schema carries `proposed_by` and
  the adjudicator does not care where a hypothesis came from, so this is a
  small addition when a provider exists that would propose interesting ones.
  `DeterministicProvider` would propose the template set back.
- **No ranking beyond survival.** Support is one probe, so counts are 0 or 1
  and ordering by score would imply a precision that does not exist. When
  probes multiply per hypothesis, this becomes a real question.

## What the mutation run found

Seven mutants, and the first run's headline (`6/7 caught`) was worth less than
it looked. Two entries read `suite crashed mid-run`, which the harness scores as
caught; both were contamination — the scenario suite and its runner were edited
while the harness was executing them, through a window where `runner.py` carried
a live `NameError`. Re-run cleanly, both are caught properly, one by three
scenarios.

That is a defect in the harness as much as an operator error:
**`run_suite` counts `<suite crashed mid-run>` as `caught = 1`.** A crash is not
evidence the suite detects a defect; it is evidence the suite fell over.
Counting it as a catch is the one direction that must not be generous, and it is
what let two invalid results read as passes.

### The survivor, and what it said about the dataset

`hypotheses: let scattered error codes still mean one failing provider`
survived everything. `_probe_provider_degradation` asks whether failures carry
one dominant error code or many different ones — and 100% of seeded UPI failures
carry `UPI_COLLECT_TIMEOUT`, so the rejecting branch never ran. Forcing the
threshold to always pass changed no observable output.

**A probe whose rejecting branch is unreachable has not been tested, however
green the suite looks.** The threshold *is* the content of the probe; without a
case where it can bite, the scenario asserting the verdict is asserting the
dataset.

`_shape_for_probes` constructs the two missing cases —
`scatter_failure_reasons` spreads failures across unrelated causes,
`spike_traffic` moves attempt volume — and HYP-04 and HYP-05 pin them. HYP-04
is now the *only* thing catching that mutant; no unit test does.

HYP-05 closed a second gap in passing: the mutant collapsing `CONTENDING` into
`SUPPORTED` had passed every scenario, and naming the first of two survivors is
exactly the single-shot answer §30 was written against.

### One gap that stays open

`evidence graph: draw an ungrounded conclusion as a root cause` is caught by
unit tests and by no scenario, and cannot currently be closed.
`DeterministicProvider` emits a `root_cause` finding only when it has evidence
ids to cite and an `uncertainty` finding when it has none, so an ungrounded root
cause is structurally unreachable from a scenario. Like ADR-0035's confidence
cap, it becomes gradeable when a provider exists that can produce one, and is
recorded here rather than closed with a scenario that would assert the rule
while never exercising it.

## Consequences

- Adjudication happens during investigation, not at read time. A verdict
  computed on read would differ between two people opening the same incident,
  which is the opposite of what an audit surface is for.
- Counts are **recomputed** on every adjudication rather than incremented, so a
  second pass cannot leave the cached totals disagreeing with the edges they
  summarise.
- Re-investigating re-tests the same candidates. `uq_hypothesis_once` makes
  that idempotent, and a rejection already announced is not announced again.
- The seeded dataset now decides three of four verdicts, which means a change to
  the seed can change what the scenarios assert. That is the right coupling —
  the alternative is verdicts that hold regardless of the data — but it means
  HYP-01 is a statement about the fixture as much as about the engine.
