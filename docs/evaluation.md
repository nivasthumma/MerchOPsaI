# Evaluation methodology

## What "deterministic" means here

> The same scenario state produces a reproducible evaluation of **observable system
> behaviour**.

It does not mean identical prose. A language model is not deterministic even at
temperature 0, so grading on wording would be measuring noise. Every check is on an
observable: tool sequence, arguments, policy decision, approval requirement, final
status, verification state, grounding, and whether an external financial effect
occurred.

Reproducibility comes from pinning: dataset seed (`20260825`), scenario file version,
tool schemas, prompt version (`investigator-v1`), and provider. Each scenario runs
against a **freshly seeded database**, so scenarios cannot contaminate one another.

## What is being measured

With `llm_provider=deterministic`, the suite measures the **control plane**: policy,
isolation, idempotency, verification, budget, replay safety. A failure is a defect in
that machinery, not model variance. This is the point — it makes regressions
attributable.

Running the same suite against `claude-opus-5` would measure something different
(agent reasoning quality) and must be reported separately. That number has not been
collected; the README says so.

## Suite composition (154)

| Category | Count | What it exercises |
|---|---|---|
| refund_policy | 25 | Approval gate, rejection, expiry, cross-merchant approver, refundable-balance and amount-limit boundaries, unmapped payments, duplicate guard |
| adversarial_security | 25 | Four injection surfaces, permission matrix, isolation matrix, malformed arguments, budget exhaustion |
| failure_unknown | 18 | Six fault types, UNKNOWN vs PARTIAL vs FAILED, re-verification, reconciliation |
| duplicate_payment | 14 | Pair/triple detection, window boundary, computed confidence, per-merchant scoping |
| payment_failure | 12 | Method isolation, hourly concentration, error attribution, no-action guarantee |
| revenue_investigation | 12 | Period comparison, method ranking, grounding, no-action guarantee |
| detection | 9 | Rule discrimination, idempotency, computed revenue-at-risk, onset accuracy, latency, incident-rooted trace, merchant scoping, lifecycle outcome |
| recovery | 13 | The §49 ordering, campaign bounds that bite, stopping applied rather than logged, and planning that moves no money |
| risk_approval | 7 | The floor rule in both directions, the one path to CRITICAL, and a two-person control that one person cannot satisfy |
| webhook | 5 | Signature, redelivery dedup, unsubscribed types, and the one that matters: a payload claiming success against provider state that says otherwise |

59 are marked `critical: true`. A critical failure is a stop condition.

**The counts are deliberately uneven.** `refund_policy` and `adversarial_security`
are large because the action path has genuinely many distinct boundaries — a payment
can be fully refunded, partially refunded, never captured, unmapped, exactly at the
limit, or one paise over, and each is a different branch. `revenue_investigation` is
smaller because, against a rule-based planner, most revenue phrasings drive the same
three tool calls; adding twenty near-identical variants would inflate the number
without adding coverage. Padding a suite to a round figure is the same dishonesty as
inflating a metric.

## Mutation testing — does the suite actually work?

> A full run is 50 mutants, each re-running the whole scenario and test suite: over half
> an hour, and memory-hungry enough to be worth running detached. Pass substrings to run
> a subset during development — `scripts/mutation_test.py webhooks detection` — but a
> filtered run is not a substitute for the full one, and CI runs all of them.

A suite reporting 106/106 proves nothing on its own. It may simply not be asserting
anything. `scripts/mutation_test.py` (`make mutants`) breaks each core control in
turn, re-runs the suite, and reports which scenarios caught the break:

```
15/15 mutations caught
```

The 15 mutations cover: the registry lookup, permission checks, merchant isolation,
the approval requirement, the amount limit, the duplicate-action guard, three
verification behaviours, argument validation, the execution budget, approval expiry,
idempotency-key derivation, the duplicate-action SAVEPOINT, and audit redaction.

"Caught" is not one thing, and the distinction matters — see *Known coverage limits*
below for how the 15 actually break down.

### What the first run found

The first mutation run scored **8/12**, with four survivors. Investigation showed:

| Survivor | Verdict |
|---|---|
| Auto-approve HIGH risk | **Bad mutant.** It changed an `approval_required` metadata field, but the runtime branches on the `Decision` enum. Semantically equivalent — rewritten to mutate the decision itself. |
| Skip argument validation | **Harness bug.** The suite crashed, and the harness read a *stale* report as if it were the result. The report is now deleted before each run, so a missing file unambiguously means a crash. |
| Drop the duplicate-action guard | **Genuine gap.** Closed by REF-25. |
| Trust the API response | **Genuine gap.** Closed by UNK-16 / UNK-17. |

Closing the two genuine gaps required a new fault type, `ACCEPTED_NOT_APPLIED` — the
provider issues a refund id but the payment's `amount_refunded` never moves. Trusting
the response reports SUCCESS; reading the payment back reports PARTIAL. It also
unblocked REF-25: without it, the *balance* check fires before the duplicate guard
(correct defence-in-depth, but it left the guard untested).

This is the strongest available evidence for the suite's value, and it is worth more
than the pass count.

### Known coverage limits

`make mutants` reports **15/15 caught**, but that headline flattens three different
kinds of catching. The measured breakdown, from the run itself:

| How the mutant is caught | Count | Mutants |
|---|---|---|
| A named scenario grades it red | 32 | permissions (5 scenarios), merchant isolation (1), auto-approve HIGH (36), amount limit (2), duplicate-action guard (1), unreadable-state verification (1), trust-the-response (3), execution budget (2), approval expiry (1), ignore the read-back (3), detection dedup (DET-02), degradation threshold (DET-01/03/09), onset volume floor (DET-09), incident outcome mapping (DET-06), webhook signature (WHK-03), webhook dedup (WHK-02), fail-closed after a bad signature (WHK-03), contradiction detection (WHK-04), risk floor rule (RSK-07), risk raising (RSK-02…05), premature execution (RSK-03…06), signature count (RSK-02…05), campaign spend bound (RCV-05), action-count bound (RCV-08), stop applied not logged (RCV-05/08), volume attribution (RCV-03), recovery-action permission (TOOL-05), ungrounded claims (OUT-03), malformed output (OUT-02/03), prose/block separation (OUT-01/05), a sent link counted as recovered (LDG-02), the unknown bucket folded away (LDG-04) |
| The suite **crashes** instead of grading | 2 | registry lookup, argument validation |
| Unit tests only — no scenario distinguishes it | 16 | idempotency-key derivation, duplicate-action SAVEPOINT, key-name branch of audit redaction, incident lifecycle legality, bulk-size grading, and six tooling controls (read/action split, link preconditions, opt-out at execution, contact dedup, notification read-back, untrusted tagging), the requires_human OR, evidence-label continuity, gross-vs-attributed reporting, resolved incidents in the at-risk figure, and per-intervention dispatch |

Read strictly, **10 of 15 mutants produce a graded scenario failure.** The other five
are still detected, and the suite is still doing real work — but "15/15 caught" and
"every control has a scenario behind it" are different claims, and only the first
one is true.

The two crashes are scenario-*reachable*: with the registry guard removed, SEC-24
drives the runtime into `AttributeError: 'NoneType' object has no attribute
'input_schema'` and the run dies. That is detection — nothing silently passes — but
the harness records `<suite crashed mid-run>` rather than SEC-24 red, so it proves
less than a graded failure would.

The three unit-only mutants each sit behind a guard that fires first:

| Mutant | Why no scenario separates it |
|---|---|
| Idempotency-key derivation (fresh key per call) | Observable only when one approval executes twice; the approval state machine prevents a second execution, and the refundable-balance precondition fires before the key is ever consulted. |
| Duplicate-action SAVEPOINT → full rollback | Same branch: it needs a key collision, which needs the balance check bypassed. Three integration tests cover it, including one that swaps in a random key to prove they measure the key and not another guard. |
| The six tooling controls | Structural, not an oversight. The deterministic planner does not compose customer contact on its own — it proposes a payment link only when a request names one — so no scenario can drive execution-time opt-out, contact deduplication, or notification read-back. Giving the planner freedom to invent customer contact would be a worse system in exchange for a better number. Each is covered directly by an integration test. |
| Bulk-size grading (treat a campaign action as standalone) | REFUND is the only executable intervention and each seeded duplicate incident yields one refundable candidate, so no scenario can build a plan with two. Reachable from the seed as soon as PAYMENT_LINK executes; covered directly by an integration test that plants a third capture. |
| Incident lifecycle legality check | The mutant lets any transition through. No scenario separates it because every transition a scenario can drive is already a legal one — the illegal moves exist only as states the application never attempts. `tests/unit/test_lifecycle.py` drives them directly and fails. |
| Audit redaction, key-name branch | The mutant disables redaction of secret-*named* dict keys. SEC-25's secret arrives inside the user's request string and is redacted by the *value-pattern* branch, which the mutant leaves intact — so SEC-25 still passes. Breaking the value branch instead does fail SEC-25 (verified: 0/1), so the scenario is real; it simply does not cover this half of `redact()`. |

Closing these would mean either a scenario that reaches a branch the safety guards
exist to make unreachable, or a second redaction mutant aimed at the value branch.
The second is worth doing; the first would be theatre. Recorded here rather than
quietly rounded up.

#### What this section got wrong twice

Both errors are kept here, because how a claim went wrong is more useful than the
corrected claim on its own.

**First:** it argued that redaction and idempotency-key derivation were pure-function
properties with no reachable path, so testing them would be theatre. Checking showed
otherwise. The raw user request is recorded on `task_created`, so anything pasted
into a request passes through `redact()` — SEC-25 exercises that. And the
idempotency `UNIQUE` constraint is reachable via `ACCEPTED_NOT_APPLIED`, which leaves
the refundable balance untouched so the precondition does not fire first; three
integration tests now cover it.

**Second:** having written those tests, it declared the gaps *closed* — "every mutant
is caught by at least one scenario", closed by UNK-18, SEC-24 and SEC-25. Only UNK-18
actually closes its mutant. The mutation output had been printing `0 scenario(s)`
next to the other two the whole time; the claim was written from intent instead of
from the run.

The pattern is the same both times: reasoning about the code where reading the output
was available. That is precisely the failure mutation testing exists to catch, so it
is corrected in place rather than quietly deleted.

Writing those tests found a genuine defect: the duplicate-action handler called
`session.rollback()`, which undoes the **entire** transaction rather than the one
failed INSERT — discarding the prior action row, the approval decision, and every
audit event written for that task. The safe path was destroying the evidence that
it had been taken. Fixed with a SAVEPOINT, and added as a 15th mutation.

Stated here so the scenario count is not read as covering more than it does.

## Grounding rate — mechanical, not judged

Every material claim is a typed `Finding`:

```
Finding { claim, kind: OBSERVED|INFERRED|RECOMMENDED, evidence_refs: [tool_call_id] }
```

```
grounding_rate = OBSERVED findings citing ≥1 resolvable tool_call_id
                 ─────────────────────────────────────────────────────
                 total OBSERVED findings
```

An OBSERVED claim with an empty or unresolvable citation is an ungrounded claim. No
LLM judge, no rubric, no human labelling. INFERRED and RECOMMENDED findings are
conclusions drawn from observations and are not required to cite directly.

## Reporting rules

Report **counts, not percentages**, at this sample size. `4/4 adversarial cases
blocked` is honest; `100% adversarial blocking` implies a precision that n=4 does not
support.

Every published number comes from an actual run. `scripts/run_scenarios.py` writes
`data/evaluation_report.json` containing the full per-check detail, the run
configuration, and the provider/adapter actually used.

## Results (measured)

```
run configuration : llm_provider=deterministic (deterministic-planner-v1)
                    payment_adapter=mock
                    dataset=synthetic-v1, seed=20260825

106/106 scenarios passed      critical: 59/59

  adversarial_security  25/25    payment_failure       12/12
  duplicate_payment     14/14    refund_policy         25/25
  failure_unknown       18/18    revenue_investigation 12/12

310 assertions
median task latency   40 ms
mean grounding rate   1.0
suite runtime         ~26 s

mutation testing      15/15 mutations caught
```

Reproducibility was verified by running the suite twice and comparing the pass/fail
vector — identical.

## Replay consistency

```
replay_consistency_rate = RE_REASON replays reproducing the original tool sequence
                          ─────────────────────────────────────────────────────────
                          total RE_REASON replays
```

Only **reasoning** divergence counts. State divergence — policy deciding differently
because the world changed, e.g. the duplicate guard denying a second refund after the
first executed — is recorded separately with its cause and does not count against
consistency.

With the deterministic planner this rate is trivially 1.0 and is therefore **not a
meaningful published metric**. It becomes meaningful only against a real model. That
measurement has not been made.

## Metrics the suite does not yet produce

Stated so the omissions are not mistaken for zeros:

- Cost per task (no token accounting on the deterministic path).
- Human intervention rate (approval is scenario-scripted, not observed behaviour).
- Recovery rate from transient failures (only three fault scenarios).
- Any model-quality metric.
