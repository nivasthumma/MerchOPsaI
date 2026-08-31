# ADR 0027 — Consuming the rules instead of restating them

**Status:** Accepted · 2026-09-01
**Governing spec:** MerchantOps §11, §32, §49, §57

## Context

Three items survived the eight-phase plan and the coverage audit. They looked unrelated. They
were the same shape: **a rule that existed in one place and was restated, or not consulted,
in another.**

- §57's retry policy was a table nothing read; the sweep and the webhook path each
  implemented it independently.
- §49's recovery could only see a paid payment link when somebody happened to settle a plan.
- §11's event store had no reader; detection read `payments`.

## Decision

### 1. The sweep asks the taxonomy which states are unsettled

`UNSETTLED` was a literal tuple in the reconciler while `app/failures.py` held its own opinion
about the same question. They disagreed about `VERIFICATION_FAILED` — the table said
RECONCILE, the sweep treated it as settled — and neither was in a position to notice.

The sweep now derives its set from the table, and the table was corrected: `VERIFICATION_FAILED`
is **ESCALATE**, not RECONCILE. Reconciling means "go and read provider state", and by the time
that code is raised the read has already happened and returned a determination. Another read
changes nothing; re-issuing is forbidden. What is left is a person.

`SUCCESS` is not reconcilable, and that now falls out of the mapping rather than needing to be
said anywhere.

### 2. `max_retries` finally does something, and the taxonomy decides

`ToolSpec.max_retries` had been on the contract since the first version and nothing read it, so
a tool could declare a retry budget and never get one.

`execute_read_tool` now honours it — but only for failures the taxonomy classes
`BOUNDED_BACKOFF`. A policy denial, an authorization failure or an unknown financial state get
one attempt regardless of what a spec declares, because the answer will be identical and, for
the last of those, repeating is the dangerous move. Reads only: this path never reaches an
action tool.

Exactly one tool declares a budget: `get_payment_status`, where a transient provider error is
plausible and re-reading is safe.

### 3. Re-verification is per action type

The defect this phase found, and the largest of the three.

`reverify_action` was written when refunds were the only action, and stayed refund-shaped after
payment links and notifications joined. Reconciling a link asked the provider about a **payment
with an empty id**, got back `"Payment  could not be retrieved"`, and left the action UNKNOWN
permanently.

So the UNKNOWN exit path — this project's signature property, the thing four ADRs argue about —
worked for exactly one of its three action types, and had done since the moment there was more
than one.

There is now a re-verifier per type and a `find_*_by_idempotency_key` per type, so an action
whose response was lost can be recovered from the only handle we kept. A type absent from the
map reports that it cannot be reconciled rather than being silently mis-verified against
whichever verifier happens to be first. A test asserts every executor has a matching
re-verifier, which is the assertion that would have caught this in Phase 5.

### 4. A paid link settles when the provider says so

`payment_link.paid`, `.expired` and `.cancelled` are subscribed. An action carrying a
`recovery_candidate_id` settles its plan on the webhook, so recovered revenue tracks reality
instead of lagging it by however long it took someone to ask.

Entity extraction puts `payment_link` **before** `payment`: a link event's payment entity is the
payment that settled it, and matching on that would reconcile the wrong action.

### 5. Detection reads the event store

`detect_provider_failure_burst` reads `webhook_events` — what the *provider* said — rather than
`payments`, which is our own record of what happened. The two are not the same thing or the same
speed, and that difference is the reason §11 puts a durable store in front of detection.

Only signature-verified events count. An unsigned delivery is stored for investigation and is not
evidence of anything.

The anomaly carries **no revenue figure**. The events name entities, not amounts, and inventing
an exposure from a count is what §22 forbids. It plans `HUMAN_ESCALATION`: a provider reporting a
run of failures is a provider conversation, not something to remedy by acting on transactions.

## Consequences

- The seeded dataset carries no provider events — it is payment history — so the burst rule is
  exercised by constructed state, in tests and in `CLS-01`/`CLS-02`. Same honesty as the
  bulk-risk path: the rule is real, the seed cannot reach it, and the scenarios say so.
- 337 tests, 167 scenarios, 69 mutants.
- Every item in `docs/spec-coverage.md` that is not credential-blocked is now closed.
