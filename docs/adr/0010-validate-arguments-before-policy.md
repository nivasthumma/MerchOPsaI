# ADR 0010 — Argument validation precedes policy evaluation

**Status:** Accepted · 2026-08-25

## Context
Found by scenario SEC-04, which supplies malformed refund arguments
(`synthetic_payment_id: 12345` as an integer). The policy engine queries the database
using tool arguments — payment ownership, refundable balance — so it received an
integer where a varchar id was expected and PostgreSQL raised
`operator does not exist: character varying = integer`.

## Decision
Validate arguments against the tool's schema in the runtime **before** the policy
engine is invoked. Reject with `TOOL_INVALID_ARGUMENT` and record the rejection.

## Rationale
The original ordering was validate-inside-execution, which is after policy. That is
both a crash and an injection surface: unvalidated model-supplied values reaching SQL
parameters. The requirement to "reject malformed input before any external call" is
only satisfied if validation is the first gate after registry lookup.

## Consequences
- Ordering in `_handle_tool`: registry lookup → argument validation → policy →
  approval → execute.
- Rejections are recorded as `tool_rejected` audit events with the validation detail.
- This was a genuine defect caught by the evaluation suite, which is the strongest
  available argument for the suite's value.
