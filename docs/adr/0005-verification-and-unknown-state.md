# ADR 0005 — Verification reads back business state; UNKNOWN is resolvable

**Status:** Accepted · 2026-08-25

## Context
A 200 response is not proof of business state. Additionally, a refund can legitimately
sit at `pending`, so trusting `refund.status` would report the ordinary path as
ambiguous.

## Decision
Verify against the **payment**: `amount_refunded` and `refund_status`, plus the refund
object's status. Classify SUCCESS / FAILED / PARTIAL / UNKNOWN.

Treat `UNKNOWN` as a **pending safety state, not a verdict**, and give it an exit:
reconcile by idempotency key via `POST /tasks/{id}/reverify`.

## Rationale
Attribution matters. If a timeout occurs and the payment shows a refund but we hold no
external reference, we cannot prove *this* action caused it — another process might
have. Reporting SUCCESS there is exactly the false verification being guarded against;
it reports UNKNOWN.

After a timeout we hold no reference, so the only way to learn whether the action
landed is to ask the provider about **our own key**. That is what makes UNKNOWN
resolvable rather than a dead end.

## Consequences
- `TIMEOUT_AFTER_SUBMIT` applies the state change *before* raising — modelling it as
  a pre-flight failure would make it a safe no-op and never exercise the dangerous case.
- Resolution is operator-driven; automatic reconciliation needs a job runner (cut).
- Every UNKNOWN result in the UI carries a Re-verify control.
