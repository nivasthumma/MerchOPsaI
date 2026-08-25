# Architectural assumptions

Required by CONTRACT §45. Every design decision in this repository rests on
assumptions that are true of *this* build environment and *this* MVP scope. When one
of them stops holding, the decision that depends on it must be revisited — so each is
recorded with what breaks if it changes.

Last verified: 2026-08-25 (81 tests, 106/106 scenarios, 15/15 mutations, demo end to
end).

## Environment

| # | Assumption | If it stops holding |
|---|---|---|
| A1 | No Anthropic credential is available in any form the SDK accepts (API key, auth token, `ant auth login` profile, workload identity), so `LLM_PROVIDER=auto` resolves to the deterministic planner. Detection covers all four; `/health` reports which was found. | Published metrics start measuring model behaviour as well as the control plane. They must then be reported as a separate table, not merged into the existing one — see `docs/evaluation.md`. |
| A2 | No Razorpay credentials are available, so `RAZORPAY_MODE` resolves to `mock`. | Re-run `make spike` first. The adapter interface, policy, approval, idempotency, verification and audit paths are unchanged; only the outbound call differs. |
| A3 | PostgreSQL 16 is reachable and the process may `DROP`/`CREATE` the schema. | `seed_data.py` and the per-scenario reseed both fail. The evaluation suite's isolation guarantee (each scenario against a fresh database) depends on this. |
| A4 | The application runs as a single process. | Rate limiting becomes approximate (the counter is in-process) and the reconciliation sweep may overlap with itself. Both are documented limitations, not defects. |

## Data

| # | Assumption | If it stops holding |
|---|---|---|
| A5 | The synthetic dataset is the analytical truth; Razorpay Test Mode is only an execution surface. | The synthetic/real boundary (§5) collapses and revenue analysis starts reading a test account that contains no organic trend. This is the assumption most expensive to get wrong. |
| A6 | Seed `20260825` reproduces byte-identically. CI asserts it. | Every published evaluation number becomes unreproducible and must be withdrawn, not re-run. |
| A7 | Only payments carrying an `external_payment_id` (21 of 589) can be acted on externally. | Nothing — refunds outside that set are rejected as `not_externally_mapped`, which is the mapping layer working. Expanding the mapped set is a data change, not a code change. |
| A8 | Revenue is **computed** from payments; there is no revenue table. | A stored aggregate would need its own reconciliation, and a disagreement between it and the payments would have no arbiter. |

## Trust and authority

| # | Assumption | If it stops holding |
|---|---|---|
| A9 | The model is never the authorization authority. Policy reads only the authenticated session and database facts. | The entire safety argument fails. `mutation_test.py` exists largely to keep this assumption honest. |
| A10 | All merchant-supplied text (customer notes, order metadata) is untrusted input, never instructions. | Injection defence rests on tagging and delimiters at the policy layer, not on the model resisting persuasion. The claim is deliberately narrow: no external call occurred and the decision was recorded. |
| A11 | The principal is resolved server-side on every request; the token carries identity only, and permissions are read from the database. | A stolen or crafted token would carry privileges. This is why permissions are not encoded in the token. |
| A12 | A provider HTTP 200 is not evidence of business state. Verification reads the *payment* back, not the refund response. | `PARTIAL` becomes unreachable and a provider that accepts a refund without applying it would be reported as SUCCESS. |

## Evaluation

| # | Assumption | If it stops holding |
|---|---|---|
| A13 | "Deterministic" means reproducible grading of observable behaviour — never identical prose. | Grading on wording would measure model noise, and no number derived from it would survive a model change. |
| A14 | A passing suite proves nothing until mutation testing shows it can fail. | The pass count becomes decorative. Note that 15/15 mutations are caught but only 10 by a graded scenario; the difference is recorded in `docs/evaluation.md#known-coverage-limits` rather than rounded up. |
| A15 | Scenario count is not a goal. Padding the suite to a round number is the same dishonesty as inflating a metric. | — |

## Scope

| # | Assumption | If it stops holding |
|---|---|---|
| A16 | One bounded agent is sufficient (§10). | Multiple agents would each need their own policy boundary; the current design gets one authority for free. |
| A17 | No Redis, Celery, Kafka, Kubernetes or containers in the MVP (§52). | Reconciliation could become a daemon and rate limiting could become shared. Both are wanted; neither is justified at this scale. |
| A18 | There are no webhooks, deliberately. | A webhook is something you are *told*; verification reads state *back*. Adding one would buy latency, not truth — the shape it would take is sketched in `docs/architecture.md`. |
