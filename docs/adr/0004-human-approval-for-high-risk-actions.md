# ADR 0004 — HIGH-risk actions require server-side human approval

**Status:** Accepted · 2026-08-25

## Decision
Every HIGH-risk action halts the agent loop and creates an expiring approval record.
Execution happens only through `approve_and_execute()`, never from the loop.

## Rationale
The approve button is not a security boundary. On approval the server re-checks: the
approval exists and is PENDING; the approver's merchant matches; the approval has not
expired (15 min TTL); full policy evaluation passes *again*; and the payment's
preconditions still hold.

Approvals expire because evidence goes stale — the payment could be refunded by
another process between recommendation and approval.

## Consequences
- `AWAITING_APPROVAL` is a first-class task state.
- Rejection is terminal and records `APPROVAL_REJECTED` with no external call.
- A cross-merchant approver is rejected before anything executes.
