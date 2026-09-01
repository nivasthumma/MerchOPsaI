# ADR 0033 — MerchantOps v2.0 governs, and the delta is small but load-bearing

**Status:** Accepted · 2026-09-01

## Context

ADR-0016 made `docs/MerchantOps.md` — version 1.0, "Enterprise Real-Time AI Revenue
Recovery & Merchant Operations Platform" — the document this repository answers to, and
`docs/spec-coverage.md` records how much of it is closed: §1–13, §15, §17–29, §31–41,
§43–58, §61–75, with §14 and §30 blocked only on absent credentials.

A version 2.0 has now been designated: "Enterprise Real-Time AI **Merchant Operations &
Revenue Recovery** Platform", 3642 lines against v1's 2668. It is not a correction. The
safety thesis is unchanged and stated more sharply — v2 §5's "AI provides reasoning;
deterministic systems provide authority" is v1 §2 in fewer words — and the fourteen
invariants in v2 §89 are the ones the codebase already enforces.

The temptation with a 37%-larger document is to treat it as a rewrite and re-audit
everything. That would be wrong. Most of the growth is exposition: v2 splits v1's dense
sections into named ones, adds worked examples (§29, §102), and restates the plane model
three times (§7, §8, §104). Read against the code rather than against v1's prose, the
substantive delta is ten subsystems.

## Decision

1. `docs/MerchantOps-v2.md` is tracked and governs. `docs/MerchantOps.md` is retained
   unedited under a `SUPERSEDED` banner, as `docs/CONTRACT.md` was before it.
2. **New code cites `v2 §N`.** Existing `§N` citations refer to v1 and resolve through
   the crosswalk below; existing `CONTRACT §N` citations still resolve through ADR-0016.
   Three specifications is one more than ADR-0016 said it would tolerate, which is why
   this ADR carries the full crosswalk and why the next spec revision should renumber
   in place rather than land as a fourth document.
3. The ten subsystems in "What is genuinely new" are the work. Nothing else in v2 opens a
   task.

## Where v2 renumbers v1

Only the sections that moved and matter. Sections whose subject and number both survived
are omitted.

| v1 § | v2 § | Subject |
|---|---|---|
| §2, §4 | §5, §6 | Core architectural principle; not-deterministic-by-design |
| §7 | §7, §8, §88, §104 | End-to-end architecture (restated four times in v2) |
| §8 | §59, §60 | Deployment; Vercel responsibilities |
| §9 | §47, §48 | Synthetic data architecture; ground truth |
| §10 | §49 | Synthetic→provider mapping |
| §11 | §9, §10, §11 | Real-time pipeline split into architecture / sources / ingestion |
| §12 | §15, §16 | Detection engine; detection examples |
| §13 | §19, §20 | Incident management; incident state machine |
| §14–§16 | §21–§23 | LLM integration, gateway, configuration |
| §17 | §24, §25 | Prompt versioning split from prompt responsibilities |
| §18, §19 | §27, §28 | Tool architecture; tool gateway |
| §20, §21 | §26, §29 | Context engineering; worked investigation |
| §22 | §34, §35 | Revenue impact engine; revenue-at-risk model |
| §23 | §36 | Recovery planner |
| §24, §25, §26 | §42, §41, §43 | Risk, policy, approval (v2 reorders: policy before risk) |
| §27, §28 | §38, §39 | Recovery budget; stopping rules |
| §29, §30 | §45, §46 | Execution manager; provider gateway |
| §31–§35 | §54, §32, §53, §51, §52 | Idempotency, verification, UNKNOWN, webhooks, reconciliation |
| §36, §37 | §31, §37 | Evidence engine; agent output (v2 folds §37 into §22's flow) |
| §39, §40, §41, §42 | §56, §72, §73, §78 | Injection defence, agent limits, versioning, promotion |
| §43–§45 | §74–§77 | Evaluation framework, scenarios, metrics, deterministic grading |
| §46, §47 | §69, §67 | Replay; audit architecture |
| §50, §51 | §63, §64 | Dashboard; incident detail |
| §52–§55 | §55, §58, §57, — | Security model, secrets, tenancy (v1 §55 authorization folds into §55) |
| §56, §57 | §70, §71 | Failure taxonomy; retry strategy |
| §58, §59, §60 | §79, §80, §81 | Observability, operational metrics, reliability targets |
| §61–§63 | §83, §82 | Enterprise expansion; why one agent now |
| §64 | §92–§100 | MVP slice becomes an eight-phase build order |
| §66 | §61 | Database core → PostgreSQL responsibilities |
| §69–§73 | §86, §87, §85, §84, §103 | The safety model and the "is it really AI" argument |
| §75 | §105 | Final architectural thesis |
| — | §91 | Repository structure — **advisory, not adopted**; see below |

## What is genuinely new

Ten subsystems v1 never asked for. Each was checked against the code, not against v1's
prose; the "today" column is what `app/` actually contains.

| v2 § | Subsystem | Today |
|---|---|---|
| §12, §13 | Event outbox; `EventPublisher`/`EventConsumer`/`EventStore` abstraction | absent |
| §14 | Merchant Digital Twin (`MerchantState`) | absent |
| §17 | Adaptive baselines — same-weekday, same-hour comparison | fixed-window (`detection/rules.py`) |
| §18 | Multivariate detection before an incident is opened | single-signal rules |
| §30 | Hypothesis engine — competing, evidence-weighted, rejectable | absent |
| §32 | Evidence graph — typed edges, not a flat list | flat `incident_evidence` rows |
| §33 | Platform-computed confidence | the model's own float, stored verbatim |
| §37, §38 | Recovery *campaign* as an entity, with its own budget | plans and candidates only |
| §40 | Strategy selection informed by historical outcomes | static planner |
| §62, §65 | Live event stream and timeline | paged list endpoints, no stream |

Two of these are correctness gaps rather than missing features, and they are the reason
this ADR exists rather than a backlog entry:

**§33.** `app/agent/runtime.py` stores `output.confidence` — a float the model chose —
onto the task. Its own comment concedes the point: "`confidence` gates nothing". v2 §33
is explicit that "LLM confidence should not be blindly trusted" and names the six inputs
a platform-owned confidence model should weigh instead. A number that gates nothing is
harmless; a number that gates nothing *and is displayed to a merchant as the system's
confidence* is a claim the system has not earned. This is the same boundary as §89 Rule 5:
if it informs a financial decision, the platform computes it.

**§18.** Every detection rule fires on one signal. v2 §18's argument is that one signal is
an anomaly and four correlated signals are an incident, and that the correlation belongs
*before* incident creation — which is also where it is cheapest, since every incident
opened is an agent budget spent.

## What is advisory and not adopted

- **§91 repository structure.** It prescribes `app/`, `api/`, `src/` with a Next.js
  frontend. This repository is FastAPI plus a React SPA (ADR-0015), deployed through a
  single entrypoint (`api/index.py`). The *module decomposition* §91 lists — agent, tools,
  detection, incidents, evidence, recovery, policy, risk, approvals, execution,
  verification, reconciliation, audit, events, providers — is already the shape of `app/`,
  with `events/` the only one missing. That one gets built; the framework swap does not.
- **§90 "Next.js / TypeScript".** Same reasoning. The stack decision is ADR-0015's and v2
  gives no argument against it.
- **§92 Phase 0 provider spike.** Already done and recorded in `docs/assessment/razorpay-spike.md`.
- **§82 multi-agent.** v2 explicitly says *not* to build five agents initially. ADR-0001
  already decided this; v2 agrees.
- **§13's "future migration to Kafka".** The abstraction gets built so the migration stays
  possible. The migration does not. v2 §13 says so directly: "The first Razorpay submission
  should not introduce Kafka simply for architectural appearance."

## Consequences

- `docs/spec-coverage.md` is now a v1 document. It stays accurate about v1 and gains a
  pointer here; a v2 coverage audit follows the work rather than preceding it.
- The §33 change is behavioural and visible: a merchant who sees `HIGH` today because the
  model emitted `0.9` may see `MEDIUM` tomorrow because only two independent signals
  support the finding. That is the intended correction, and it needs a scenario asserting
  the model cannot talk its way up.
- Adding an outbox means every business-state write that should emit an event now has two
  places to get it wrong — the write and the enqueue. The transaction is what makes that
  safe, so the outbox insert must share the caller's session, never open its own.
