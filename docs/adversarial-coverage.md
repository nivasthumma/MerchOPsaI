# §20 — the twenty mandatory adversarial scenarios, audited

**Measured 2026-09-07 against `3ebe32a`.** Method at the bottom; re-runnable.

MerchantOps §20 names twenty adversarial scenarios and calls them mandatory.
This is what the suite actually covers, counted rather than asserted — the
distinction the plan itself draws in P0-10.

| # | Scenario | Scenarios | Verdict |
|---|---|---|---|
| 1 | duplicate refund | 52 (39 critical) | covered |
| 2 | expired approval | 1 (critical) | covered |
| 3 | revoked approval | 2 (critical) | covered |
| 4 | wrong tenant | 9 (8 critical) | covered |
| 5 | wrong payment mapping | 3 (2 critical) | covered |
| 6 | prompt injection | 10 (8 critical) | covered |
| 7 | malformed provider response | 1 (critical) | covered |
| 8 | duplicate webhook | 5 (4 critical) | covered |
| 9 | **out-of-order webhook** | **0** | **gap — see below** |
| 10 | provider timeout | 14 (10 critical) | covered |
| 11 | worker retry | 13 (10 critical) | covered |
| 12 | stale action | 0 scenarios | covered by `test_durability.py`, not as a scenario |
| 13 | budget exceeded | 5 (1 critical) | covered |
| 14 | customer attempt limit | 0 scenarios | covered by `test_recovery.py`, not as a scenario |
| 15 | UNKNOWN verification | 10 (8 critical) | covered |
| 16 | replay side-effect attempt | 1 | covered |
| 17 | unauthorized action | 13 (8 critical) | covered |
| 18 | prohibited model tool request | 1 (critical) | covered |
| 19 | malformed tool response | 4 (3 critical) | covered |
| 20 | LLM unavailable | 0 scenarios | partially — see below |

## The one real gap: out-of-order webhooks

§14 lists "out-of-order events" among the deliveries webhook handling must
cope with, and **nothing tests it**. `tests/integration/test_webhooks.py`
covers signature validation, redelivery and deduplication, unsubscribed event
types, unknown entities and contradictions — but not a delivery that arrives
after a later one.

The behaviour is almost certainly right *by construction*: `process_event`
never reads the payload for truth, it re-reads provider state through the
adapter, so a stale event triggers a fresh read that reflects current state
whatever order events arrived in. That is the "a webhook decides *when* to
look, never *what* was found" rule doing its job.

But nothing proves it, and the property is exactly the kind a later change
breaks silently — the day someone reads `payload["status"]` because it is
right there, a stale `payment.failed` arriving after a settled refund would
regress a SUCCESS. An untested invariant is a comment.

## Two covered, but not as scenarios

**12 (stale action)** and **14 (customer attempt limit)** are genuinely
exercised — the abandoned-claim branch in `tests/integration/test_durability.py`
and the `max_attempts_per_customer` stopping rule in
`tests/integration/test_recovery.py`. They are listed as gaps in the scenario
column because §20 sits beside §21's *deterministic agent evaluation*, and a
unit test does not grade a tool sequence or a final state the way a scenario
does. Whether that distinction matters here is a judgement call; it is recorded
rather than smoothed over.

## 20 (LLM unavailable) is partial

The fallback exists and is reported: `Settings.resolved_llm_provider` returns
`deterministic` when no credential is present, `/health` publishes which is
active and why, and the UI states it before anyone can act. What is not
covered is a provider that is *configured and then fails mid-run* — and it
cannot be, honestly, in a build where the Anthropic provider has never executed
against the API (README, known limitation 2).

## Method

    scenarios: data/scenarios/scenarios.yaml
    matched on: id, description, request, fault, notes, expect

Keyword matching over the scenario corpus, so the counts are a floor rather
than a precise attribution — a scenario may exercise a control its description
does not name. A count of zero is the claim worth acting on, and each zero
above was then checked by hand against the test suite, which is where the three
qualified verdicts came from.
