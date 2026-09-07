# ADR 0002 — Synthetic data is the analytical truth; Test Mode is the action surface

**Status:** Accepted · 2026-08-25 · **mechanism amended by
[ADR-0033](0033-the-mapping-is-a-table.md), 2026-09-07**

The *boundary* below is unchanged and still the decision: synthetic data for
investigation, Test Mode for execution, joined by an explicit mapping layer.
What changed is how that layer is implemented — it was two nullable columns on
`payments` and is now `provider_mappings`, a table carrying provider and
environment with both directions UNIQUE. The consequences below are marked
where they no longer describe the code.

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
- ~~Five synthetic payments carry `external_payment_id`~~ — twenty-one now do,
  and the mapping is a row in `provider_mappings` rather than a column
  (ADR-0033). Only the mapped subset can be executed, which is unchanged.
- `resolve_external_payment()` is the sole synthetic→provider bridge and enforces
  merchant ownership. **The agent can never name a provider id.** Still true; it
  now delegates to `app.integrations.mapping.resolve`, which is also what the
  verification path uses — a read and a write that resolved an id differently
  was how "verified the wrong payment" could have happened.
- Refunds outside the mapped set are rejected as `not_externally_mapped`. A
  second refusal joins it: `mapped_to_other_environment`, for a payment mapped
  in Test Mode while the process runs against live credentials. That case was
  previously indistinguishable from "unmapped".
- The boundary is stated in the README rather than buried.
