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

## Suite composition (159)

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

> A full run is 88 mutants, each re-running the whole scenario and test suite: over two
> hours, and memory-hungry enough to be worth running detached. Pass substrings to run
> a subset during development — `scripts/mutation_test.py webhooks detection` — but a
> filtered run is not a substitute for the full one, and CI runs all of them.

A suite reporting 167/167 proves nothing on its own. It may simply not be asserting
anything. `scripts/mutation_test.py` (`make mutants`) breaks each core control in
turn, re-runs the suite, and reports which scenarios caught the break:

```
87/88 mutations caught      complete run, 2026-09-08, 2h06m, tree 3ebe32a
88/88                       after the survivor's test, verified individually
```

Every run writes `data/mutation_report.json` — the per-mutant result, the scenarios
that graded each one red, the commit it measured, and whether the run was complete or
filtered. It exists so the published figure comes out of a file something produced
rather than out of somebody's memory of a terminal that has since scrolled away;
`scripts/check_counts.py` reads it and fails when the README disagrees. A *filtered*
run is refused rather than compared: its ratio measures a subset, and letting
`mutation_test.py webhooks` set the project's score is the exact substitution that
check exists to prevent. The file is git-ignored for the same reason
`data/evaluation_report.json` is — it measures a tree rather than describing one.

The 88 mutations span policy, verification, the runtime, actions, governance,
reconciliation, tools, webhooks, detection, metrics, messages, tenancy, failure
classification, observability, durability, migrations, versioning, the recovery
ledger, agent output, risk, approval, the incident lifecycle, audit, and the provider
mapping — one per control, each named for the defect it introduces rather than for the
line it edits.

"Caught" is not one thing, and the distinction matters — see *Known coverage limits*
below for how the 88 actually break down.

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

`make mutants` reports **87/88 caught** (88/88 once the survivor's test is counted),
but that headline flattens three different kinds of catching. The breakdown below is
read out of the run's own table rather than estimated — the harness names the scenarios
each mutant turned red, and a row naming none is a mutant no scenario distinguishes:

| How the mutant is caught | Count |
|---|---|
| A named scenario grades it red | **40** |
| The suite **crashes** instead of grading | **2** |
| Unit tests only — no scenario distinguishes it | **45** |
| Nothing caught it | **1**, now closed |

Read strictly, **40 of 88 mutants produce a graded scenario failure.** The other 47 are
still detected and the suite is still doing real work — but "87/88 caught" and "every
control has a scenario behind it" are different claims, and only the first is true.

The 45 fall into four groups, and only the last is an oversight:

**Read-side aggregates the scenario suite cannot drive** — metrics (three), the ledger
(two), failure classification (two), governance (two), observability (two), versioning,
budget-vs-host-timeout. A scenario grades a tool sequence and a final state; it does not
read `/metrics` back and check the shape of a number.

**Branches a safety guard reaches first** — idempotency-key derivation and the
duplicate-action SAVEPOINT (the refundable-balance check fires before the key is
consulted), the incident lifecycle's legality check (every transition a scenario can
drive is already legal), the key-name branch of audit redaction (SEC-25's secret arrives
in the request string and is caught by the value-pattern branch, which the mutant leaves
intact).

**Structural, and correctly so** — the seven tooling controls. The deterministic planner
does not compose customer contact on its own; it proposes a payment link only when a
request names one. So no scenario can drive execution-time opt-out, contact
deduplication, or notification read-back. Giving the planner freedom to invent customer
contact would be a worse system in exchange for a better number.

**New surface with no scenarios yet** — the three mapping guards, the two reconciliation
repair passes, and three detection rules added since the last scenario pass. These are
the ones genuinely worth closing, and they are listed here rather than folded into the
structural bucket, which is where an inconvenient number goes to be forgotten.

### The survivor

**`reconciliation: escalate actions that already settled`** deleted the settled check
inside `should_escalate`, and 626 tests did not notice. Three of that function's four
callers make the check redundant — `escalate_exhausted` filters settled rows out in
SQL, and both sweep call sites are already inside an `if state in UNSETTLED` branch.
The fourth is not redundant: `reverify` calls it unconditionally, *after* deciding what
the read found.

So an operator who presses Re-verify on an UNKNOWN action four times and gets a real
SUCCESS on the fifth crosses the attempt limit on the attempt that resolved it. The
same action is then marked COMPLETED with "Re-verification resolved the action:
SUCCESS" and handed to a human as "still unestablished". A finished refund on the
escalation queue is how a queue stops being read.

`test_a_manual_reverify_that_finally_succeeds_does_not_escalate` closes it, verified
the only way a single mutant can be: applied by hand → red, reverted → green. **88/88
is therefore two measurements and is stated as two** — 87 from the complete run, one
from a hand-verified mutant added after it. Adding a test cannot un-catch a mutant, so
the 87 still hold; a full re-run against this exact tree has not been done, and the
number is not presented as though it had.

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
| Three failure-taxonomy classifications | The retryability of a policy denial, the class of an unrecognised code, and the derivation of the registry version are properties of a TABLE. No scenario drives a lookup against it, because nothing in the runtime branches on `may_retry()` yet — the sweep and the webhook path each implement §57 independently. Covered directly by unit tests; the honest fix is to wire the runtime to the table, which is work rather than a test. |
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

Read out of `data/evaluation_report.json` and the mutation log, 2026-09-08. This block
had been left at `106/106 · 310 assertions · 15/15 mutations` long after every one of
those numbers had moved — which is why `scripts/check_counts.py` now gates the
published figures against what the tree measures.

```
run configuration : llm_provider=deterministic (deterministic-planner-v1)
                    payment_adapter=mock
                    dataset=synthetic-v1, seed=20260825

167/167 scenarios passed      critical: 110/110

  adversarial_security  34/34    recovery              14/14
  detection             10/10    refund_policy         28/28
  duplicate_payment     16/16    revenue_investigation 19/19
  failure_unknown       20/20    risk_approval          7/7
  payment_failure       14/14    webhook                5/5

568 assertions
median task latency   45 ms
mean grounding rate   1.0

mutation testing      87/88 caught in a complete 2h06m run
                      88/88 with the survivor's test, verified individually
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
