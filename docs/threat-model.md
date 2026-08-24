# Threat model

Scope: an AI agent with read access to merchant business data and the ability to
request one financial action (refund) that executes only after human approval.

## Trust boundaries

```
UNTRUSTED   model output · customer/order/product free text · tool result text
TRUSTED     authenticated principal · database facts · policy engine · adapter
SECRET      provider credentials · API keys   (never enter model context)
```

The single most important boundary: **model output is untrusted input to the
application.** Everything the model produces is a *request*, evaluated by
deterministic code before it has any effect.

## Threats and controls

| # | Threat | Control | Verified by |
|---|--------|---------|-------------|
| T1 | Prompt injection via user input | Authorization derived from session, never from text; policy outside the model | `test_injection_in_customer_notes_does_not_cause_refund` |
| T2 | Prompt injection via customer/order metadata | Tagged `untrusted` at the tool boundary; wrapped in `<untrusted_merchant_data>` delimiters; output validated structurally | `test_injected_text_is_tagged_untrusted_and_delimited`, `test_untrusted_rendering_wraps_in_delimiters` |
| T3 | Unauthorized financial action | Permission check before risk evaluation; HIGH risk always requires approval | `test_unauthorized_user_cannot_refund` |
| T4 | Tool parameter manipulation | Strict schemas, `additionalProperties: false`, type/range/enum validation **before** policy | `test_malformed_arguments_rejected_before_external_call` |
| T5 | Replay / duplicate financial action | Server-derived idempotency key + `UNIQUE` constraint; row reserved before the call; duplicate-action policy guard | `test_double_approval_produces_one_refund` |
| T6 | Race between two approvals | `UNIQUE(idempotency_key)` INSERT is the serialization point; conflict means "already attempted" | `test_double_approval_produces_one_refund` |
| T7 | Credential leakage into prompts or logs | Secrets live only in the adapter; `redact()` scrubs keys and provider token patterns from every audit payload | `test_secrets_are_redacted_from_traces` |
| T8 | Cross-merchant data access | Merchant predicate in every SQL query **and** an explicit policy gate; API returns 404 not 403 | `test_cross_merchant_order_read_denied`, `test_cross_merchant_refund_denied`, `test_cross_merchant_approver_rejected` |
| T9 | Excessive agent permissions | Six tools, four read-only; no shell, no SQL, no URL construction, no dynamic dispatch | `test_model_cannot_call_unregistered_tool` |
| T10 | Malformed or malicious provider response | Adapter normalises; malformed bodies raise rather than being read as success | `MALFORMED_RESPONSE` fault |
| T11 | Model hallucination presented as fact | Typed `Finding`; OBSERVED claims must cite a resolvable `tool_call_id`; grounding rate computed | `test_every_observed_finding_is_grounded` |
| T12 | False verification (API 200 ≠ business state) | Read back the payment, not the refund object; attribution required before SUCCESS | `test_verification_success_reads_the_payment`, `test_lost_response_yields_unknown_then_resolves` |
| T13 | Runaway loop / cost exhaustion | 12 tool calls, 8 turns, 60s; terminates `ABORTED_BUDGET` | `test_budget_terminates_runaway_loop` |
| T14 | Approval replay after conditions change | Approval TTL (15 min); full policy re-evaluation and precondition re-check at execution | `test_expired_approval_is_invalid` |
| T16 | Unsettled action never resolved | Reconciliation sweep with bounded attempts; escalation queue surfaces what it cannot settle | `test_unsettleable_action_escalates_and_stops` |
| T17 | Reconciliation re-issuing a financial action | Sweep re-reads state only; reconciles by idempotency key; no action path exists | `test_sweep_settles_unknown_without_reissuing` |
| T15 | Financial side effect during replay | Runtime halts at approval; HIGH tools have no read-path implementation; outcome asserted | `test_re_reason_makes_no_financial_side_effect` |

## The injection claim, stated precisely

This project does **not** claim the model resists prompt injection. That claim would
be unverifiable and, with a deterministic planner, meaningless.

The claim is narrower and testable:

> Injected instructions in merchant data cannot cause an unauthorized or unapproved
> financial action, because authorization is not decided by the model.

The dataset plants four injection sites, including a customer note demanding an
unapproved ₹50,000 refund, attached to a customer involved in the duplicate — so any
legitimate investigation necessarily pulls the hostile text into context. The
assertion is at the policy layer: **no external call occurred, and the decision was
recorded.**

## Residual risks (accepted)

- **Authentication is a header stand-in.** The principal is resolved server-side from
  the users table, but there is no token verification. Production needs a real IdP.
- **No rate limiting.** Single-user local deployment.
- **Audit is append-only by application convention**, not enforced by database
  permissions or WORM storage.
- **`UNKNOWN` settles at sweep cadence, not instantly.** `scripts/reconcile.py`
  settles unsettled actions and escalates what it cannot; with no always-on worker,
  latency is the cron interval. An action can no longer sit unsettled unnoticed, but
  it is not settled in real time.
- **Mock adapter in the default build.** Its security properties are identical (same
  policy, approval, idempotency, verification) but it exercises no real TLS,
  authentication, or provider error surface.
