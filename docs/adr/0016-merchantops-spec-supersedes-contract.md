# ADR 0016 — MerchantOps.md supersedes the implementation contract

**Status:** Accepted · 2026-08-31

## Context

The repository was built against `docs/CONTRACT.md`, the "Master Implementation
Contract". Every ADR, `docs/architecture.md`, `docs/architecture/assumptions.md` and most
module docstrings cite it by section number — `CONTRACT §20`, `CONTRACT §36`, and so on.
Those citations are load-bearing: they are how a reader of `app/policy/engine.py` learns
*why* the gate order is what it is.

A new specification, `MerchantOps.md`, has been designated the governing document. It is
not a revision of the contract. It is a wider re-framing of the same system, with the
same safety thesis (§75: "enough autonomy to reason, never enough authority to bypass
deterministic controls") but a substantially larger surface — a real-time event pipeline,
a detection engine, incident management, a recovery planner with budgets and stopping
rules, webhook ingestion, and revenue-recovery measurement. Its section numbering is
entirely different from the contract's.

This creates a citation problem. Roughly 200 `CONTRACT §N` references exist in the source
and docs. Rewriting them all to MerchantOps numbering would mean a very large diff across
every safety-critical file, at real risk of mis-citation, for no behavioural gain. Leaving
them unexplained would mean new readers cannot tell which document a `§20` refers to.

## Decision

1. `MerchantOps.md` is copied into the repository at `docs/MerchantOps.md` and tracked. It
   had never been under version control in any branch.
2. `docs/CONTRACT.md` is retained, unedited apart from a `SUPERSEDED` banner at the top.
   It remains the recorded justification for ADRs 0001–0015.
3. **New code cites MerchantOps §.** Existing `CONTRACT §N` citations are left in place and
   resolve through the crosswalk below.
4. Where the two documents disagree, MerchantOps.md governs. Where MerchantOps.md is
   silent and the contract is not, the contract's constraint stands unless an ADR retires
   it — the contract's narrower scope decisions (no Kafka, no Redis, no microservices) are
   still the right calls and MerchantOps.md §8 and §63 independently agree.

## Crosswalk

| CONTRACT § | MerchantOps § | Subject |
|---|---|---|
| §1 Mission | §1, §5 | Business problem |
| §2 Product positioning | §74 | Submission positioning |
| §3 Built vs designed | §61, §63 | Scope discipline |
| §4 Architecture principle | §2, §4, §75 | LLM proposes, software decides |
| §5 Synthetic/real boundary | §9, §9.2 | Data architecture |
| §6 External payment mapping | §10 | Synthetic→provider mapping |
| §7 Razorpay spike | §30 | Provider adapter |
| §8 Non-negotiable principles | §71, §75 | What stays deterministic |
| §9 MVP workflow | §64 | Vertical slice |
| §10 Agent runtime | §40 | Agent budget |
| §11 Agent context rules | §20 | Context engineering |
| §12 Typed tool registry | §18 | Tool list |
| §13 Tool contract | §19 | Tool gateway |
| §14 Tool result / Finding | §36, §37 | Evidence model, output schema |
| §15 Synthetic dataset | §9.1 | Analytical data |
| §16 Seeded incidents | §12, §13 | Detection, incidents |
| §17 Revenue investigation | §21, §22 | Investigation loop, revenue calc |
| §18 Duplicate investigation | §21 | Investigation loop |
| §19 Risk classification | §24 | Risk engine |
| §20 Policy engine | §25 | Policy engine |
| §21 Approval workflow | §26 | Approval engine |
| §22 Razorpay adapter | §30 | Provider adapter |
| §23 Refund execution | §29 | Execution manager |
| §24 Idempotency | §31 | Idempotency |
| §25 Verification engine | §32 | External verification |
| §26 Final verification states | §32, §33 | UNKNOWN as a state |
| §27 Audit trail | §47 | Audit architecture |
| §28 Replay | §46 | Replay |
| §29–§32 Evaluation | §43 | Evaluation framework |
| §33 Security scenarios | §44, §45 | Example evaluations |
| §34 Failure model | §56 | Failure taxonomy |
| §35 Retry rules | §57 | Retry architecture |
| §35A Fault injection | — | Repository extension; no MerchantOps equivalent |
| §36 Prompt injection defence | §39 | Injection defence |
| §37 Secret management | §53 | Secret management |
| §38 Merchant isolation | §54 | Multi-tenant isolation |
| §39 Observability | §58, §59 | Traces, metrics |
| §40 Streamlit UI | §50, §51 | Dashboard, incident page |
| §41 API surface | §65 | Vercel API surface |
| §42 Database model | §66 | Database core |
| §44 Technology choices | §8 | Deployment architecture |
| §47 Implementation strategy | §63, §64 | One agent, one slice |
| §43, §45, §46, §48, §49 | — | Process instructions; no equivalent |

## What has no antecedent

These MerchantOps sections describe subsystems the contract never asked for, and which
therefore do not exist in the codebase. They are the substance of the gap-closure work
(see `docs/gap-closure-plan.md`):

§11 event pipeline · §12 detection engine · §13 incident management · §23 recovery planner ·
§27 recovery budget · §28 stopping rules · §34 webhook processing · §35 reconciliation
engine · §48 operations loop · §49 recovery measurement · §50 dashboard · §51 incident
detail · §60 SLOs · §67 source-of-truth rules

## Consequences

- A reader encountering `CONTRACT §20` in `app/policy/engine.py` can resolve it here
  without archaeology, and the file itself needs no edit.
- Two specifications now exist in `docs/`. The banner and this ADR are what keep that from
  being ambiguous; if a third ever appears, this pattern should be replaced with a single
  numbered spec rather than extended.
- The contract's §35A fault-injection seam has no MerchantOps equivalent. It is retained:
  it is what makes the `failure_unknown` scenario category testable, and losing it would
  cost 18 scenarios.
