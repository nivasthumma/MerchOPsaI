# MerchantOps.md coverage — what is closed and what is not

**Audited:** 2026-09-01, against `feat/incident-spine`.
**Updated:** 2026-09-01 — the closable items below are now closed.

`docs/gap-closure-plan.md` reports all eight phases delivered, and it is. That is not the
same claim as "every section of the specification is closed", and this file exists so the
two are not confused.

## Closed

§1–13, §15, §17–29, §31–41, §43–58, §61–75. The architecture the document describes — the
detection and incident spine, the tool gateway, computed risk, dual approval, recovery
planning with budgets and stopping rules, idempotent execution, independent verification,
UNKNOWN as a resolvable state, webhook ingestion as evidence, the recovery ledger, the
agent output schema, the failure taxonomy and correlation traces — is built, tested, and
covered by graded scenarios and mutants.

## Blocked on credentials, and unclosable here

| § | What is missing | Why |
|---|---|---|
| §14 | "The agent genuinely uses an LLM" | No Anthropic credential in any form the SDK accepts. Runs on `DeterministicProvider`. |
| §30 | Real Razorpay Test Mode execution | No Razorpay credential. `MockAdapter` is a deterministic local double; policy, approval, idempotency, verification and audit are identical on both paths. |
| §42 | Model governance as a *comparison* | The mechanism exists — CI gates on critical scenarios and a merge is blocked if any fails. What has never happened is running two models and comparing, because there is only one path available. |

## Closed since the audit

Ordered by how much each one matters.

### 1. ~~`tenant_id` does not exist~~ — CLOSED (ADR-0025)

`tenants` table; `tenant_id` on `merchants`, `users` and `webhook_events`; a second
isolation boundary checked outermost-first, with `MERCH_C` seeded in the same tenant as
`MERCH_A` so the two boundaries are separately testable.

<details><summary>the original finding</summary>

Both sections name `tenant_id` alongside `merchant_id`. The system has only `merchant_id`,
and every isolation check is written against it. That is correct for one-merchant-per-tenant
and wrong the moment a tenant owns two merchants — the model has no way to express it, and
every `WHERE merchant_id = :m` would need a second clause.

Not a defect today. A modelling gap that gets more expensive the longer it is left.
</details>

### 2. Seven of §66's tables — `agent_messages` CLOSED (ADR-0026), six deliberate

| table | status |
|---|---|
| `roles`, `permissions` | permissions are a JSON list on `users`. Works; not a table, so not queryable or auditable as a set. |
| `agent_messages` | **CLOSED.** One row per message as it is appended, including the final answer, with untrusted content flagged and secrets redacted. `GET /tasks/{id}/messages`. |
| `policies`, `policy_decisions` | policy is code, decisions live in `audit_logs`. Deliberate — per-merchant configurable policy is explicitly future work. |
| `provider_mappings` | columns on `payments`. Deliberate, documented in ADR-0002. |
| `evaluation_scenarios`, `evaluation_runs` | a YAML file and a run id on `evaluation_results`. Deliberate. |

The remaining six are deliberate and documented. `roles`/`permissions` as tables is the only
one that would change behaviour rather than shape, and per-merchant configurable policy is
explicitly future work.

### 3. ~~Most of §59's operational metrics are not produced~~ — CLOSED

Produced: detection latency, investigation latency, tool latency, actual revenue recovered,
UNKNOWN counts.

Not produced: root-cause accuracy, revenue-at-risk accuracy, recovery precision, recovery
rate, policy violations, unauthorised actions, verification latency, agent cost, provider
latency.

`app/metrics.py` produces eleven of them and reports the other three as **unavailable with a
reason**: root-cause accuracy and revenue-at-risk accuracy need labelled ground truth that a
production incident does not carry, and agent cost needs token accounting this build has no
path to. A figure computed from nothing is worse than a blank — the blank prompts the
question and the number closes it. `GET /metrics/operational`.

### 4. ~~One SLO is unmeasured~~ — CLOSED

The policy engine is now timed at its outer boundary, so the number covers the database
reads too. All four objectives report a measured value and whether they hold, at
`GET /metrics/objectives`; an objective with nothing to measure reports `null`, not `pass` —
nothing having happened is not the same as everything having been fast.

### 5. ~~Two §65 routes have no equivalent~~ — CLOSED

`GET /approvals` and `GET /actions/{id}`. The queue marks an expired approval as expired:
it stays `PENDING` in the database until someone tries to use it, and showing it as
actionable work would be showing work that cannot be done.

### 6. ~~Temperature is never set~~ — RESOLVED AS A DEVIATION

§16 asks for `temperature: 0 / lowest supported`, and it is **not implementable on the
configured model**. Sampling parameters — `temperature`, `top_p`, `top_k` — were removed on
the Claude Opus 5 family; sending `temperature` returns a 400. Checked against the current
API reference rather than assumed.

`output_config.effort` is the control that replaced it, and §16's intent is served by pinning
effort and the model id, both recorded on every run under §41. Determinism was never fully
available in any case: the contract already notes that a model is non-deterministic at
temperature 0, which is why replay records divergence rather than asserting its absence.

The deviation is now stated in the provider itself and asserted by a test, so it cannot be
mistaken for an omission and cannot be quietly undone.

### 7. Carried forward from earlier phases

- Detection reads `payments`, not the event store (§11). ADR-0017 §1.
- Nothing in the runtime branches on `app/failures.py` (§57). ADR-0024.
- No `payment_link.paid` subscription, so a paid link is found only at settle time (§49).
  ADR-0023.

## What is still open

Only the three carried-forward items in §7 above, all of them documented in their own ADRs
and none of them load-bearing for the safety argument:

- detection reads `payments`, not the event store (§11)
- nothing in the runtime branches on `app/failures.py` (§57)
- no `payment_link.paid` subscription (§49)

Plus the two credential-blocked sections, which cannot be closed in this environment.

Everything else in `MerchantOps.md` is built, tested, and covered by graded scenarios and
mutants.
