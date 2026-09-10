# ADR-0053 — Financial controls, transaction boundaries and provenance

**Status:** Accepted · 2026-09-10
**Source:** *MerchantOpsAI — End-to-End Senior Architecture & AI Engineering Review*,
remediated on `remediation/architecture-review` (from `2f1d036`).

## Context

The review's verdict was that the architecture is right and must not be rewritten:
AI reasons, deterministic systems hold authority. What it asked for was the gap
between a strong control-plane design and a demonstrably complete financial
outcome path to be closed. Auditing the tree against it found that some of that
gap was real defect rather than missing feature:

- **The live refund call sent `X-Payment-Idempotency`.** Razorpay's refund APIs
  read `X-Refund-Idempotency`. Every live refund went out with no provider-side
  idempotency.
- **A live payment link could never have been created.** Its `reference_id` was
  the 64-character key; Razorpay caps the field at 40.
- **Ambiguous provider answers read as FAILED.** A 5xx, a 409 (same key in
  flight) and a 2xx body missing its fields were a definite failure on the live
  path, and a malformed body raised a `KeyError` after the provider had accepted
  the refund. FAILED invites a retry of something that may have moved money.
- **UNKNOWN could not be resolved on the live path.** Both key lookups returned
  `None` unconditionally.
- **Row-level security was shed by the first mid-request commit.** The scope is
  `SET LOCAL`, applied once when `session_scope` opened. `checkpoint()` commits,
  the session continues in a new transaction, and an empty scope is
  unrestricted. Every worker task ran unscoped for the same reason.
- **Provider calls ran with row locks held**: webhook processing on the request
  path (a provider retry blocked on the uncommitted `event_id`), the
  reconciliation sweep (locks on every action it had settled so far), plan
  settlement, verification after the provider answered.
- **A replay's approval could be executed**, re-issuing a historical financial
  action against the live provider; and a replay with no recorded result for a
  provider read made the read live.
- **No run said how it was produced.** There was no fallback from a failed model
  and no way to tell planner output from model output.
- **An action could be hidden from every queue**: a timeout before submission
  left status UNKNOWN beside a FAILED verification, listed nowhere.

## Decision

### Provider correctness (P0)

`LiveTestModeAdapter` classifies every write into exactly one outcome:

| Answer | Reading | Exception |
|---|---|---|
| 2xx with id and amount; 4xx refusal | applied / refused | — / `ProviderError` |
| connection never opened | not sent | `ProviderTimeout(submitted=False)` |
| read/write timeout, any 5xx, 409, unreadable 2xx, "reference already attempted" | **ambiguous** | `ProviderAmbiguous` (a `ProviderTimeout`, `submitted=True`) |

Ambiguous goes to UNKNOWN and reconciliation, never to FAILED. Refunds send
`X-Refund-Idempotency` (key format checked before sending); links send a 40-char
`reference_id`. UNKNOWN is resolvable live: a refund by listing the payment's
refunds and matching the key recorded in `notes` (paged, bounded), a link by
`reference_id`. Reads never leak `httpx` exceptions into verifiers.

**Proven by** `tests/unit/test_razorpay_contract.py` against recorded response
shapes, and end to end through `execute_refund` in `test_remediation.py`. **Not
proven against Razorpay Test Mode**: no credentials exist in this environment.
`scripts/razorpay_spike.py` is the path that does that.

### The transaction boundary (P1)

> No external call occurs while a transaction holding writes is open.

`app/boundaries.py` makes that checkable. Session events mark a session as
holding writes (a flush, or DML / `FOR UPDATE` through it) until its root
transaction ends; every provider call asserts it holds none. `strict` raises a
`BaseException` (so no `except Exception` can turn it into UNKNOWN), `warn` logs,
`off` skips. **The whole pytest suite and the scenario evaluation run strict.**

Fixed to satisfy it: webhook processing moved off the request path; the
reconciler commits per action; `reverify_action` commits before reading;
`settle_plan` reads links before writing; the agent loop commits before each
model turn and before each provider-reading tool; the provider reference is
committed before verification reads.

### Tenancy survives a commit (P1, security)

An `after_begin` session hook applies the bound scope to **every** transaction.
Workers bind an explicit `ExecutionContext` and push its scope onto the
transaction already open.

### UNKNOWN (P0)

`SUBMITTED → UNKNOWN → re-verify → SUCCESS | FAILED | escalated`. The sweep
reads and never re-issues. A timeout before submission ends FAILED with
`TOOL_TIMEOUT`, not UNKNOWN. A submitted action whose request died before
verification (`SUBMITTED`, no outcome) is reconciled like an abandoned claim, and
the Action Center lists it under UNKNOWN.

### Unified idempotency (P1)

`idempotency_records` (merchant-scoped, row-level secured): tenant, merchant,
operation, business key, request hash, status, external reference, resource,
expiry. Same key + same request → the original outcome; same key + different
request → `IDEMPOTENCY_CONFLICT`, before any row is reserved or provider called.
Kept beside, not instead of, `agent_actions.idempotency_key` and the provider's
own key.

### Approval snapshot (P1)

An approval records `policy_version`, `policy_decision`, `policy_rule` and
`policy_input_hash`. Execution refuses a payload that no longer hashes to what
was approved, re-evaluates current policy, and records both policy versions.
`revoke` withdraws a pending approval; replays' approvals are refused.

### Execution context and actors (P1)

`app/context.py`: `ExecutionContext(actor_type, actor, tenant, merchant,
permissions, correlation, task, incident)`. Every audit row carries
`actor_type` ∈ HUMAN · AGENT · WORKER · WEBHOOK · SYSTEM, read from the context,
never from a payload.

### Events (P1)

`event_outbox.category` ∈ DOMAIN · INTEGRATION · UI · NOTIFICATION, derived from
the type. New DOMAIN events (`refund.requested/submitted/verified`,
`recovery.completed`) are written in the mutation's transaction and are not
swallowed on failure. INTEGRATION events (`razorpay.<type>`) are written at
ingest. The UI stream serves only UI and NOTIFICATION frames; v2 §62's list is
unchanged. UI mirror frames remain best-effort within the transaction, as
`test_the_audit_trail_survives_a_broken_event_stream` requires.

### Webhooks (P1)

validate → persist raw (malformed bodies too, as INVALID) → deduplicate (a
reused id with a different body is reported as a CONFLICT) → ACK → worker
(`webhooks` job) claims with a lease, processes as WEBHOOK under the owning
merchant's scope, at most one attempt per delivery per pass, dead-letters as
FAILED after five.

### AI provenance (P1)

`agent_tasks.ai_mode` ∈ AI_SUCCESS · AI_FAILED_FALLBACK · AI_UNAVAILABLE_FALLBACK ·
DETERMINISTIC_ONLY. Fallback happens only on `ModelUnavailable` (SDK connection,
timeout, rate-limit, 5xx) — never on our own bugs — and is audited
(`llm_fallback`) before the planner's turn. `configuration_version` hashes the
governing settings. No `retrieval_version` is recorded because there is no
retrieval component.

### Evidence (P1)

Evidence and findings carry OBSERVED · DERIVED · INFERRED · RECOMMENDED ·
EXECUTED · VERIFIED. VERIFIED only for a settled read-back (SUCCESS or FAILED).
DERIVED, EXECUTED and VERIFIED must cite a tool call, as OBSERVED always did.

### Financial outcome

A created link is an attempt; a `partially_paid` link contributes the paid share
and stays ATTEMPTED; only a paid link is RECOVERED. The ledger now reports
`recovered_minor` split into `recovered_captured_minor` (a customer paid a
recovery link — recovered revenue) and `recovered_refunded_minor` (a verified
refund — money returned). The Command Center reports agent and provider posture;
a mock provider is labelled as one.

## The truth hierarchy

| Fact | Authoritative layer | Everything else is |
|---|---|---|
| Did money move at the provider? | **Provider** (read back through the adapter) | our projection of it |
| What we asked for, under which key | `agent_actions` + `idempotency_records` | — |
| What verification last established | `agent_actions.verification_state` | a cache of a provider read |
| What was recovered, attempted, unknown | recovery ledger (`recovery_candidates`) | derived; never from a link's creation |
| What a provider told us | `webhook_events` (evidence) | never a statement of state |
| What MerchantOps did, and who did it | `audit_logs` (append-only, trigger-enforced) | — |
| Who may act | `users`/`roles`/`role_permissions`, re-read at execution | never the token's claims alone |

## The action lifecycle, mapped rather than rewritten

| Review's state | Where it lives |
|---|---|
| PLANNED | approval `PENDING` |
| AUTHORIZED | approval `APPROVED` (snapshot recorded) |
| EXECUTING | action `PENDING` (claim committed) |
| SUBMITTED | action `SUBMITTED`, reference committed |
| VERIFYING | `SUBMITTED` with no verification outcome |
| SUCCESS / FAILED / UNKNOWN | `CONFIRMED` / `FAILED` / `UNKNOWN` (+ `verification_state`) |
| ESCALATED | `escalated = true` (a recorded decision, not a status) |

## Consequences

- A dev database needs `make migrate` (`b7e2c41d9a53`).
- Webhook deliveries are processed by the worker, not the request. Without a
  worker they wait in RECEIVED; `/command-center` shows how many.
- `make eval` and the suite enforce the boundary; `warn` in production reports
  the remaining known violation instead of hiding it.

## Not done, and why

- **The event drain still delivers notifications while holding its claimed rows**
  (`FOR UPDATE`). The network channels now run the boundary guard, so in
  production it is reported, not silent; fixing it means a claim-then-commit
  drain. P1.
- **The agent runtime is not decomposed** into session / executor / budget /
  persistence classes. The review asked for that incrementally and without
  behavioural change; this change added behaviour (fallback, replay refusal,
  commits) and deliberately did not also move code. P1.
- **A queued task's original correlation id is not carried to the worker.** P2.
- **Intelligence evaluation against a real model has not been run**: no model
  credential exists here. The control evaluation is the only one published. P1.
