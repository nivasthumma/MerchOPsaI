# MerchantOps.md coverage — what is closed and what is not

**Audited:** 2026-09-01, against `feat/incident-spine`.
**Updated:** 2026-09-07 — see *What changed on 2026-09-07* at the end. One row
of the §66 table below is now wrong in the other direction: `provider_mappings`
is a table.

> **This file audits v1.** `docs/MerchantOps-v2.md` now governs (ADR-0033), and its
> section numbers are not these. Everything below remains accurate about v1; the §
> references resolve through ADR-0033's crosswalk.
>
> Against v2, four of the ten new subsystems are built: §11–13 the event spine,
> §62/§65 the live stream, §33 computed confidence (ADR-0034), §18 multivariate
> correlation — the last two graded by scenarios and mutants per ADR-0035. Still
> open: §14 digital twin, §17 adaptive baselines, §30 hypothesis engine, §32
> evidence graph, §37–38 recovery campaigns, §40 strategy selection from history,
> §20's full state machine, and the UI timeline that consumes `/events`.
>
> A full v2 audit belongs after that work, not before it — this file exists
> because "all phases delivered" and "every section closed" are different claims,
> and writing a v2 audit now would repeat the confusion it was created to prevent.

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
| §42 | The model-vs-model *measurement* — the RULE is closed | ADR-0028. The promotion rule is implemented and tested against §42's worked example: a candidate scoring 24/25 against a baseline's 23/25 is refused for one critical regression. `make compare` is a gate that exits non-zero. What has never happened is running two models, because there is only one path available. |

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
| `roles`, `permissions` | **CLOSED** (ADR-0047). Three tables — a catalogue derived from the tool registry, tenant-owned roles, and the join. `users.role_id` replaces the JSON list. `GET /access-review` is the artefact SOC 2 asks for quarterly. |
| `agent_messages` | **CLOSED.** One row per message as it is appended, including the final answer, with untrusted content flagged and secrets redacted. `GET /tasks/{id}/messages`. |
| `policies`, `policy_decisions` | policy is code, decisions live in `audit_logs`. Deliberate — per-merchant configurable policy is explicitly future work. |
| `provider_mappings` | **CLOSED (ADR-0033).** Was two columns on `payments`; now a table with provider and environment on the row and both directions UNIQUE. The columns remain as the mock provider's own store, and `check_consistency` asserts the two agree. |
| `evaluation_scenarios`, `evaluation_runs` | a YAML file and a run id on `evaluation_results`. Deliberate. |

`roles`/`permissions` was the only one of these that would have changed behaviour rather
than shape, and it is now closed. The remaining five are deliberate and documented, and
per-merchant configurable policy is still explicitly future work.

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

### 7. ~~Carried forward from earlier phases~~ — CLOSED (ADR-0027)

All three, and they turned out to be one shape: a rule that existed in one place and was
restated, or not consulted, in another.

- Detection now has a rule that reads the event store (§11) — signature-verified events only,
  no invented revenue figure, escalated rather than acted on.
- The reconciliation sweep derives its unsettled states from `app/failures.py` (§57), and
  `ToolSpec.max_retries` is honoured for transient failures only.
- `payment_link.paid` is subscribed, and an action from a recovery candidate settles its plan
  on the webhook (§49).

**One real defect fell out of it.** `reverify_action` was refund-shaped and stayed that way
after payment links and notifications joined, so reconciling a link asked the provider about a
payment with an empty id and left the action UNKNOWN permanently. The UNKNOWN exit path worked
for one of three action types, and had since the moment there was more than one.

## What is still open

**Only the credential-blocked sections.** §14 (the agent genuinely using an LLM) and §30 (real
Razorpay Test Mode execution) cannot be closed in this environment, and §42's model governance
mechanism exists but has never had two models to compare.

Everything else in `MerchantOps.md` is built, tested, and covered by graded scenarios and
mutants.

The standing limitations in the README are a different list: those are honest properties of
what was built (a sweep rather than a daemon, confidence as a display value, 21 of 589 payments
externally mapped), not gaps against the specification.

---

## What changed on 2026-09-07

Work against the enterprise production plan. Listed by which specification
section it moves, because "closed" above was a claim made before several of
these existed.

| § | Was | Now |
|---|---|---|
| §6 | Two nullable columns on `payments`, no provider, no environment, no uniqueness in either direction | `provider_mappings`, a table, with both directions UNIQUE and one resolver (ADR-0033) |
| §7 | `/trace/{correlation_id}` — everything one **operation** touched | `GET /payments/{id}/lifecycle` — everything that touched one **payment**, across every correlation id it spans |
| §11 | `/health` reported configuration and was used as a liveness *and* readiness probe | `/liveness` (no I/O) and `/readiness` (per-component verdicts, 503 on a required failure), with operational detail gated behind a token |
| §12 | Three rules: baseline deviation, duplicates, provider burst | Five. Added failure-code spikes (diagnostic, claims no revenue) and unusual refund activity (claims the excess only) |
| §26 | Escalation re-derived from `verify_attempts >= 5` at two call sites | A recorded decision with a timestamp, a stored backoff schedule, and a repair pass that guarantees it is made |

The operations console the plan asks for in P0-03 through P0-08 — Action
Center, Command Center, the incident decision workspace, agent activity built
from recorded rows — did not exist and now does. That is product surface rather
than a specification section, so it is not in the table.

**§20's out-of-order webhooks — the one gap
[`docs/adversarial-coverage.md`](adversarial-coverage.md) named — is closed.** The
behaviour was right by construction, because `process_event` re-reads provider state
rather than believing the payload; what was missing was anything proving it. Three
tests now do: a stale failure arriving after a settled refund, both arrival orders of
the same pair reaching the same state, and the event store keeping the provider's
timestamp separate from arrival time. Verified against a hand-applied "believe the
payload" defect, which turns three tests red.

### Still not closed, and why

- **§14, §30, §42** — unchanged. Blocked on credentials, as above.
- **§8's worker topology** — still a cron sweep, still a deliberate scope
  decision. `/readiness` now reports when the sweep has stopped running, which
  makes the trade-off visible rather than silent.
