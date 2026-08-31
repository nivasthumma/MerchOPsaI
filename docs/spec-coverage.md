# MerchantOps.md coverage — what is closed and what is not

**Audited:** 2026-09-01, against `feat/incident-spine` @ `df90932`.

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

## Open, and closable

Ordered by how much each one matters.

### 1. `tenant_id` does not exist (§11, §54)

Both sections name `tenant_id` alongside `merchant_id`. The system has only `merchant_id`,
and every isolation check is written against it. That is correct for one-merchant-per-tenant
and wrong the moment a tenant owns two merchants — the model has no way to express it, and
every `WHERE merchant_id = :m` would need a second clause.

Not a defect today. A modelling gap that gets more expensive the longer it is left.

### 2. Seven of §66's twenty-five tables are absent

| table | status |
|---|---|
| `roles`, `permissions` | permissions are a JSON list on `users`. Works; not a table, so not queryable or auditable as a set. |
| `agent_messages` | **the LLM conversation is never persisted.** Tool calls and audit events are; the messages between them are not. Replay reconstructs from the trace instead. |
| `policies`, `policy_decisions` | policy is code, decisions live in `audit_logs`. Deliberate — per-merchant configurable policy is explicitly future work. |
| `provider_mappings` | columns on `payments`. Deliberate, documented in ADR-0002. |
| `evaluation_scenarios`, `evaluation_runs` | a YAML file and a run id on `evaluation_results`. Deliberate. |

`agent_messages` is the one worth building: without it, "what did the model actually see"
cannot be answered after the fact.

### 3. Most of §59's operational metrics are not produced

Produced: detection latency, investigation latency, tool latency, actual revenue recovered,
UNKNOWN counts.

Not produced: root-cause accuracy, revenue-at-risk accuracy, recovery precision, recovery
rate, policy violations, unauthorised actions, verification latency, agent cost, provider
latency.

Several of these need ground truth the dataset does not carry (root-cause accuracy), and
several are trivial (verification latency, provider latency are already timed and thrown
away). They are not distinguished today, which is itself the problem.

### 4. One SLO is unmeasured (§60)

Detection < 60s is graded by `DET-05`. Zero unauthorised executions and zero unverified
success claims are graded across the suite. **Policy decision < 200ms is measured nowhere** —
the policy engine is not timed at all.

### 5. Two §65 routes have no equivalent

`GET /api/approvals` (the pending-approval queue as its own resource) and
`GET /api/actions/:id`. Both are reachable indirectly through the task they belong to, so
nothing is impossible — but an approvals queue is a screen an operator wants, and it is
currently only assemblable client-side.

### 6. Temperature is never set (§16)

§16 asks for `temperature: 0 / lowest supported`. `app/llm/anthropic_provider.py` sets
`max_tokens`, `thinking={"type": "adaptive"}` and an effort level, and never sets
temperature. That is probably correct — adaptive thinking constrains temperature — but the
deviation is undocumented, which means nobody can tell whether it was a decision or an
omission.

### 7. Carried forward from earlier phases

- Detection reads `payments`, not the event store (§11). ADR-0017 §1.
- Nothing in the runtime branches on `app/failures.py` (§57). ADR-0024.
- No `payment_link.paid` subscription, so a paid link is found only at settle time (§49).
  ADR-0023.

## The honest summary

The document's *architecture* is closed. Its *instrumentation* is not: metrics, one SLO, one
model field, two routes, and a tenancy dimension nobody has needed yet. None of it is load
bearing for the safety argument, and all of it is the sort of thing that is easy to leave
undone and awkward to explain later.
