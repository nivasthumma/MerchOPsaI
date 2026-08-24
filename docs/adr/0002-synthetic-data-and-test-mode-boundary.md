# ADR 0002 — Synthetic data is the analytical truth; Test Mode is the action surface

**Status:** Accepted · 2026-08-25

## Context
The original design had the agent analyse Razorpay Test Mode data directly. A test
account contains no organic revenue trend, no UPI failure concentration, and no
naturally occurring duplicate payments.

## Decision
```
Synthetic dataset  → investigation + evaluation
Razorpay Test Mode → execution + state verification
```
joined by an explicit mapping layer.

## Rationale
Presenting test-account data as merchant behaviour would misrepresent the entire
investigation layer. Separating the two lets the investigation be rich and seeded with
reproducible incidents, while external execution stays genuinely external.

## Consequences
- Five synthetic payments carry `external_payment_id`; only those can be executed.
- `resolve_external_payment()` is the sole synthetic→provider bridge and enforces
  merchant ownership. **The agent can never name a provider id.**
- Refunds outside the mapped set are rejected as `not_externally_mapped`.
- The boundary is stated in the README rather than buried.
