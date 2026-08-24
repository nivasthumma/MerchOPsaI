# ADR 0008 — Amendments to the Master Implementation Contract

**Status:** Accepted
**Date:** 2026-08-25

The contract at `docs/CONTRACT.md` is the authoritative spec. Its §Instruction
block requires that conflicts be documented in an ADR before behaviour changes.
Seventeen amendments were applied before implementation began.

## Blocking defects

| # | Section | Defect | Amendment |
|---|---------|--------|-----------|
| 1 | §13 | `idempotency_key` was a model-supplied tool argument. On retry the model emits a fresh key, uniqueness misses, refund executes twice — defeating §24. | Key removed from the tool schema; derived server-side as `sha256(merchant_id\|external_payment_id\|action_type\|approval_id)`. |
| 2 | §42 | No `agent_actions` table, so §24's "persist before execution" was unimplementable. `refunds` is the business entity, not the attempt record. | Table added with `UNIQUE(idempotency_key)` and the reserve→call→reconcile order made mandatory. |
| 3 | §26/§41 | `UNKNOWN` had no exit path: no endpoint, no UI control, no job. | `POST /tasks/{id}/reverify` added; §26 gains a "Resolving UNKNOWN" clause. |
| 4 | §35A | Timeout/UNKNOWN scenarios were unproducible — these are adapter-level faults, not dataset facts. | New §35A defines a scenario-driven `FaultInjector` seam with six fault types. |
| 5 | §14 | "Evidence grounding" was graded (§29) and required deterministic (§30) but `evidence` was an untyped list. | Typed `Finding{claim, kind, evidence_refs}` contract added; grounding becomes mechanically computable. |
| 6 | §10/§34 | No loop or cost budget anywhere. | Budget added (12 tool calls / 8 turns / 60s) plus `BUDGET_EXCEEDED`. |
| 7 | §15/§31/§33 | Merchant isolation (§38) was untestable — single-merchant dataset, no scenario. | Dataset requires ≥2 merchants; cross-merchant scenario added to §33. |

## Corrections

| # | Section | Change |
|---|---------|--------|
| 8 | §25 | Verification predicate named explicitly: read back `payment.amount_refunded` / `refund_status`, not just the refund object — a `pending` refund would otherwise report the common path as ambiguous. |
| 9 | §28 | Replay split into `PLAYBACK` (deterministic) and `RE-REASON` (model in the loop). Divergence recorded as `REPLAY_DIVERGED` and reported as `replay_consistency_rate` rather than asserted away. |
| 10 | §36 | Injection defence given a mechanism: `untrusted` tagging at the tool boundary, explicit delimiters, structural output validation, authorization re-derived from session. |
| 11 | §31 | Category counts reconciled with §33 — §33 listed five required security scenarios, §31 allotted four. Adversarial 4→5, revenue 5→4, total still 25. |
| 12 | §53 | Dangling reference to an undefined "critical evaluator returning `needs_human`" replaced with a defined `critical: true` scenario flag. |
| 13 | §45 | Inspection steps made conditional on an existing repository — this is greenfield, so the steps would have produced a vacuous report. |
| 14 | §48 | "5–6 week plan" vs six listed weeks resolved to six. |
| 15 | §48/§57 | Audit trace moved to Week 1, matching §57 item 12; Week 2 keeps hardening/redaction. |

Amendments 16–17 are the §24 execution-order block and the §13 rationale text,
recorded inline above.

## Not amended

The five-agent future-state architecture, the Streamlit choice, the 25-scenario
target, and the built-vs-designed framing are unchanged — they were correct.
