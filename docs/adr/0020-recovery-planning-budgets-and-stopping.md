# ADR 0020 — Recovery planning, budgets, and stopping as a first-class capability

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §22, §23, §24, §27, §28, §49

## Context

Detection found problems and the agent investigated them, but nothing turned a root cause
into a bounded set of things that could be done about it. §23's flow — incident, affected
transactions, eligibility, expected recovery, risk, intervention candidates — had no
implementation, and with it went §27's budgets and §28's stopping rules.

## Decision

### 1. Planning ends at candidates

§23's flow stops at *intervention candidates*. So does this module. There is no bulk
executor anywhere: a candidate is acted on through §29's existing single-action path, with
the same policy, approval, idempotency and verification gates as any other financial
action.

A second way to move money would be a second thing to get right, and the first one took
four ADRs. `dispatch_candidate` adds exactly one thing an ordinary task does not have —
the §27 and §28 bounds are checked immediately before dispatch — and then hands over.

### 2. The §49 ordering is a claim about the world, not a display convention

    revenue at risk  >=  eligible recovery  >=  expected recovery

The first implementation summed the full failed volume and produced INR 34,467 eligible
against INR 29,261 at risk. An eligible figure larger than the at-risk figure says a
merchant can recover more than the incident cost them. That is a lie a dashboard would
tell confidently.

The gap has a name. Detection computes at-risk as the value of the *excess* failures — the
ones above the method's own baseline. Some payments fail on the best of days, and their
value was never at risk from this incident. So volume is attributed before it is counted:

    attributable = min(1, revenue_at_risk / total_failed_volume)
    eligible     = eligible_failed_volume x attributable
    expected     = eligible x baseline_success_rate

For a refund both factors are 1: a duplicate charge is owed back in full, and multiplying a
debt by a conversion probability would understate it.

`expected_recovery_basis` travels with the figure so the number cannot be rendered without
its reasoning, and §49 keeps expected and actual in different columns so the two can never
be reported as one. `actual_recovery_minor` is populated only from a verified SUCCESS — an
UNKNOWN action has not been shown to have moved anything.

### 3. Rounding once, rather than clamping

Worth recording because the first fix was wrong in an instructive way.

Per-candidate rounding accumulates: thirty-three shares rounded up by half a paise each put
eligible one paise above at-risk. The fix was to clamp the total to the at-risk figure.
That held the ordering — and hid the reason it might break. With attribution disabled the
clamp still produced a valid-looking ordering, so **no assertion could distinguish a
correct figure from a wrong one trimmed to fit**. A mutation that claimed the whole failed
volume was at risk survived the entire suite.

Computing the total once from the exact aggregate is both the better arithmetic and the
version that can be checked. `RCV-03` now catches that mutant.

The general lesson: a guard that forces an invariant to hold is not the same as the
invariant holding, and it can make the difference untestable.

### 4. STOP and ESCALATE are different claims

§28 says stopping is a first-class capability. Collapsing its two outcomes loses the one
that matters.

    STOP      the campaign is finished or not worth continuing.
              Nothing is wrong; nobody needs to be woken up.

    ESCALATE  the campaign cannot safely decide for itself.
              A human has to look.

Budget exhaustion is a STOP — the bound did its job. A provider that has gone away, an
action graded above the automation ceiling, or evidence that does not support the plan is
an ESCALATE: the system has met something it is not entitled to resolve alone.

Every rule returns a disposition and the caller acts on it. `apply_stop` is deliberately
separate from `evaluate_stopping_rules`, so that asking "may I proceed?" cannot halt a
campaign by asking, and a stopped plan stays stopped even if the bound that stopped it is
later lifted.

### 5. Budgets are copied onto the plan and checked before every action

§27's bounds are on a campaign that runs over time, so checking them at planning time only
says what was true at planning time. `check_budget` runs immediately before each dispatch
and answers about the state right now — including the *prospective* spend, because checking
after the fact is a report, not a limit.

The bounds are copied onto the plan at creation rather than read live. A merchant raising
their limit mid-campaign must not silently widen a plan already in flight; the bounds are
part of the decision that authorised it.

Committed spend counts everything attempted, not only what succeeded. A budget that counted
only confirmed successes would let an unbounded number of failures through.

### 6. Bulk size becomes a real risk input

ADR-0019 deferred §24's `bulk_size` factor because no tool took more than one target. The
planner is what creates multi-action campaigns, so it arrives here: more than one financial
action in one campaign is CRITICAL, which §24 states directly ("Bulk refund → CRITICAL").

The distinguishing feature of breadth is that a single mistake repeats itself, which is why
it is its own dimension rather than a multiple of value. `bulk_size` is the count of
executable candidates in the plan, not one — grading each action as if it stood alone is
exactly how a bulk campaign escapes the class the spec assigns it.

CRITICAL then meets §28's risk ceiling and the campaign escalates rather than running.
Automated recovery does not perform bulk refunds; a human does.

### 7. One incident per order, not per pair

A Phase 1 defect that Phase 4's measurement work surfaced.

`detect_duplicate_payments` emitted one anomaly per *pair*. For two captures that is right.
For three it produced three overlapping incidents, each claiming the full amount at risk,
totalling 3x an exposure that is really 2x. Nothing on the shipped dataset triggers it —
the seeded triple sits outside the detection window — so this was a latent overcount rather
than an observed one. A revenue figure that is wrong only on data we happen not to have is
still wrong.

Detection now emits one incident per order: the earliest capture is the legitimate payment,
everything after it is excess, and the exposure is what remains unrefunded across the
excess.

### 8. Opt-out governs contact, not debt

§28 lists "customer has opted out" as a stopping condition, so `customers.contact_opted_out`
exists and the planner reads it. It blocks PAYMENT_LINK, CUSTOMER_NOTIFICATION, RETRY and
SUBSCRIPTION_RETRY — and deliberately does not block REFUND. Money owed back reaches the
customer through the payment rail, not through marketing consent.

Two customers who actually own failed UPI payments in the planted degradation are opted out
explicitly in the seed. The index-based rule alone opted out ~6% of customers and none of
them happened to be candidates, which would have left the rule written but never exercised.

## Consequences

- Five of §23's seven interventions have no tool behind them. A candidate proposing one is
  a real recommendation, recorded and ranked, with `executable=False` — and dispatch
  refuses it by name (`not_executable`) rather than failing obscurely. They become
  actionable with §18's tools.
- Because REFUND is the only executable intervention, and each duplicate incident yields
  one refundable candidate on the shipped dataset, the bulk path is exercised by
  test-constructed state rather than by the seed. It becomes reachable from the seed the
  moment PAYMENT_LINK is executable — 33 candidates in one plan.
- `settle_plan` reads outcomes back from verified actions; the candidate does not decide
  what happened.
- 201 tests, 135 scenarios, 33 mutants. The bulk-grading mutant is caught by unit tests
  only — no scenario distinguishes it, for the reason above.
