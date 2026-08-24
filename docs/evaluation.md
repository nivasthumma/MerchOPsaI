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

## Suite composition (106)

| Category | Count | What it exercises |
|---|---|---|
| refund_policy | 25 | Approval gate, rejection, expiry, cross-merchant approver, refundable-balance and amount-limit boundaries, unmapped payments, duplicate guard |
| adversarial_security | 25 | Four injection surfaces, permission matrix, isolation matrix, malformed arguments, budget exhaustion |
| failure_unknown | 18 | Six fault types, UNKNOWN vs PARTIAL vs FAILED, re-verification, reconciliation |
| duplicate_payment | 14 | Pair/triple detection, window boundary, computed confidence, per-merchant scoping |
| payment_failure | 12 | Method isolation, hourly concentration, error attribution, no-action guarantee |
| revenue_investigation | 12 | Period comparison, method ranking, grounding, no-action guarantee |

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

A suite reporting 106/106 proves nothing on its own. It may simply not be asserting
anything. `scripts/mutation_test.py` (`make mutants`) breaks each core control in
turn, re-runs the suite, and reports which scenarios caught the break:

```
15/15 mutations caught
```

Mutations cover: the registry lookup, permission checks, merchant isolation, the approval requirement,
the amount limit, the duplicate-action guard, four verification behaviours, argument
validation, the execution budget, approval expiry, idempotency-key derivation, and
audit redaction.

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

Every mutant is now caught by at least one scenario. The three that were once
unit-test-only are closed:

| Was uncovered | Closed by |
|---|---|
| Verification reporting SUCCESS on an unreadable state | **UNK-18** — the read-back itself fails |
| Audit redaction disabled | **SEC-25** — a secret in the user's request must not survive into the trail |
| Registry lookup removed | **SEC-24** — the agent requests an unregistered tool |

An earlier version of this document argued that redaction and idempotency-key
derivation were pure-function properties with no scenario-reachable path, and that
building scenarios for them would be theatre. That was wrong on both counts, and
checking rather than assuming is what showed it:

- The raw user request is recorded on the `task_created` audit event, so anything a
  user pastes into it passes through `redact()`. That is a real path, and SEC-25
  exercises it.
- The idempotency `UNIQUE` constraint *is* reachable — but only after an attempt
  that leaves the refundable balance untouched, because the balance precondition
  fires first in the ordinary path. `ACCEPTED_NOT_APPLIED` produces exactly that
  state, and three integration tests now cover the branch.

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
