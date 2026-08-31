# Gap-closure plan — CONTRACT.md → MerchantOps.md

**Status:** in progress — phases 0 and 1 delivered
**Governing spec:** `MerchantOps.md` (supersedes `docs/CONTRACT.md`)
**Baseline:** `master` @ `e151ebd`, 2026-08-26 — 106/106 scenarios, 15/15 mutants, 95 tests

> The baseline test count is 95, measured. The README says 81; that figure is stale and
> predates work already on `master`.

## What this plan is closing

The build implements the **right half** of MerchantOps.md's loop to a high standard:
policy → approval → idempotency → execution → verification → reconciliation → audit.
Sections 2–4, 15–16, 19, 25–26, 31–33, 35, 39–40, 43–46 and 53–55 are substantially met.

It does not implement the **left half** — observe → detect → incident — nor the recovery
and measurement layer that closes the loop back to the merchant. MerchantOps.md §64's
vertical slice begins at `event → detection → incident`; this build begins three boxes
later, at "a user asks a question" (`POST /tasks`).

That is the shape of the gap. Everything below is ordered so the spine gets built first
and the rest hangs off it.

## Standing constraint

Every phase extends `data/scenarios/scenarios.yaml` **and** `scripts/mutation_test.py`.
The 106/15 pair is this project's credibility; adding four subsystems without adding
mutants would silently dilute both numbers while leaving them nominally unchanged. A
phase is not done when its code works — it is done when a deliberate break in it is
caught.

## Blocked regardless of phase

Two limitations are environmental and no amount of implementation clears them:

- No Anthropic credential → the agent runs `DeterministicProvider`. MerchantOps.md §14
  ("the agent genuinely uses an LLM") and §42 (model governance) can be *built* but not
  *demonstrated*.
- No Razorpay credential → `MockAdapter`. §30's adapter boundary is real; the outbound
  call is not.

Phases 0–8 are all buildable without either. State this in any submission rather than
letting the architecture imply otherwise.

---

## Phase 0 — Supersession bookkeeping · S · ~0.5d — **DONE**

Blocking for citation hygiene, not for code.

Fourteen ADRs, `docs/architecture.md`, `docs/architecture/assumptions.md` and most
module docstrings cite `CONTRACT §N`. Mass-rewriting those citations would be a large,
risky diff across every safety-critical file for zero behavioural gain.

Instead:

1. Move `MerchantOps.md` into the repo and track it. It has never been in any branch.
2. Keep `docs/CONTRACT.md`, marked `SUPERSEDED` — it is the recorded justification for
   every existing ADR and deleting it orphans them.
3. Write `docs/adr/0016-merchantops-spec-supersedes-contract.md` carrying a
   **§-number crosswalk table** (CONTRACT § → MerchantOps §). New code cites
   MerchantOps §; existing citations resolve through the table.

## Phase 1 — The incident spine · L · ~4–5d — **DONE**

**Closes §11, §12, §13. Unblocks phases 3 and 7.**

The single largest gap and the one that changes what the product *is*.

- Tables: `events` (durable store, §11 field list — `event_id`, `payload_hash`,
  `correlation_id`, `schema_version`), `incidents`, `incident_evidence`.
- `app/detection/` — deterministic/statistical, per §12. The LLM does not inspect raw
  events; detection reduces them to anomalies first.
- Rule per §12: `current_success_rate < baseline - threshold`, windowed by payment
  method, emitting `PAYMENT_DEGRADATION`.
- Incident lifecycle state machine per §13, with the terminal set. Transitions are
  deterministic control-plane logic — the model never moves an incident.
- Link `agent_tasks.incident_id`; incident creation dispatches an investigation through
  the existing `AgentRuntime` unchanged.
- Routes: `/api/incidents`, `/api/incidents/:id`, `/api/incidents/:id/investigate`.

**No new data needed.** `scripts/seed_data.py:172-177` already plants a UPI degradation
(success drops to ~36% inside the planted window) and deliberately keeps the comparison
windows clean around it. Detection has real signal to find on day one — the seed was
written for an investigation the agent was *told* about; this phase makes the system
find it unaided.

New eval category `detection`: detection latency (§60: < 60s), no false positive on a
flat window, illegal lifecycle transition rejected.

**Delivered** — see [ADR-0017](adr/0017-detection-engine-and-incident-spine.md).
`app/detection/` (rules + sweep), `app/incidents/` (lifecycle + manager), `incidents` and
`incident_evidence` tables, five routes, 9 detection scenarios, 5 new mutants, 27 tests.

**One deviation from this plan:** the `events` table is *not* built here. In this system a
`payments` row is the event, and an events table would have no producer until webhook
ingestion exists — a table nothing writes to is the skeleton component both specs warn
against. It moves to phase 2, where it has a writer. Reasoning in ADR-0017 §1.

## Phase 2 — Webhook ingestion · M · ~2–3d

**Closes §34, completes §35.**

- `/api/webhooks/razorpay` must sit *outside* the bearer-token dependency
  (`app/api/security.py:133`) and authenticate by HMAC signature instead. That is a
  distinct trust path and the place this phase is most likely to go wrong.
- Signature → `event_id` dedup → durable `webhook_events` row → processing.
- **The webhook is evidence, not authority** (§34). It must feed `reconcile()`, never
  write `agent_actions.verification_state` directly. `app/verification/reconciler.py`
  already has the correct shape: add a webhook-evidence source alongside the existing
  API read-back.
- Provider event contradicting internal state → `RECONCILIATION_INCIDENT` surfaced as a
  real incident (§35), now that Phase 1 gives it somewhere to live.
- Extends `RazorpayAdapter` with `get_provider_event`.

Side benefit: this substantially closes README limitation #4. Webhook delivery makes
settlement near-real-time without introducing Redis or Celery — the sweep stays as the
backstop rather than the only mechanism.

Scenarios: duplicate delivery (same `event_id` twice → exactly one state change),
invalid signature rejected, webhook/internal contradiction → incident.

## Phase 3 — Risk engine + policy expansion · M · ~2d

**Closes §24, §25. Ordered before recovery so recovery actions have a risk model.**

Risk today is a static per-tool constant (`app/policy/engine.py:56`). §24 wants it
computed from financial value, reversibility, customer impact, bulk size and
uncertainty.

- Add `CRITICAL` to `RiskLevel` and `REQUIRE_DUAL_APPROVAL` to `PolicyDecision`.
- **Design rule, non-negotiable:** computed risk may only *raise* risk above the tool's
  declared class, never lower it. A computed risk that can downgrade is a path for
  model-influenced input to reduce a control, which is precisely the boundary §75
  forbids. The declared class is the floor.
- **Fix the dual registration while here.** `ToolSpec.risk_class` /
  `required_permissions` and the engine's `TOOL_RISK` / `TOOL_PERMISSIONS` dicts declare
  the same facts twice, and policy silently wins: `runtime.py` passes
  `spec.risk_class.value` into `PolicyContext`, then `evaluate()` overwrites it with
  `TOOL_RISK[tool_name]`. Today they agree. A future tool added to the registry but not
  to the dict is denied as `unregistered_tool` — fail-closed, so not a live
  vulnerability, but it is a trap and Phase 4 adds nine tools straight into it. Make the
  registry the single source.

Mutants: force computed risk to `LOW`; make the floor rule permit a downgrade. Both must
be caught.

## Phase 4 — Recovery planner, budget, stopping rules · L · ~4d

**Closes §22, §23, §27, §28.**

- Tables: `recovery_candidates`, `recovery_actions`, `recovery_budgets`.
- Deterministic eligibility and expected-recovery calculation. §22 is explicit: the
  calculation engine owns the number, the LLM explains it. This is where "the model must
  not invent financial figures" acquires enforcement rather than remaining a prompt
  instruction.
- Intervention types per §23: `RETRY`, `PAYMENT_LINK`, `CUSTOMER_NOTIFICATION`,
  `SUBSCRIPTION_RETRY`, `REFUND`, `HUMAN_ESCALATION`, `NO_ACTION`.
- Per-incident budget (§27): max recovery amount, max actions, max attempts per
  customer, max duration. Enforced in the policy engine.
- Stopping rules (§28) as an explicitly evaluated predicate returning `STOP` /
  `ESCALATE` — not an implicit loop exit.

Note the axis distinction: `config.py:91-94` bounds *agent compute* (12 tool calls, 8
turns, 60s). §27 bounds *financial exposure*. They are unrelated limits and conflating
them would let a cheap agent run spend an unbounded amount.

## Phase 5 — Tool registry expansion · M · ~3d

**Closes §18 — 6 tools today, 15 specified.**

- Investigation: `get_failure_breakdown`, `get_payment`, `get_customer`.
- Recovery: `calculate_recovery_candidates`, `generate_payment_link`,
  `send_customer_notification`.
- Verification: `get_payment_status`, `get_provider_event`, `reconcile_transaction`.

**The trap:** `generate_payment_link` and `send_customer_notification` are
state-changing actions with irreversible external side effects — a sent notification
cannot be unsent. They must take the reserve → execute → verify path in
`app/tools/actions.py`, **not** the read-tool path in `registry.py:execute_read_tool`.
Routing them as reads would give the model two un-idempotent external effects with no
action record, no idempotency key and no verification. `get_customer` also returns
customer notes — free text, so `Evidence.untrusted=True` is mandatory (§39).

## Phase 6 — Agent output schema · M · ~2d

**Closes §37, completes §20 and §36.**

Today `runtime.py:_derive_findings` synthesises findings deterministically after the run,
and `agent_tasks.recommendation` (`models.py:184`) is declared but never written
anywhere. §37 wants the model to emit
`{intent, findings, recommendation, confidence, requires_human}`, backend-validated.

Keep both, deliberately:

- Deterministic `OBSERVED` findings stay. They are what makes grounding computable
  without an LLM judge, and that is the project's strongest measurement.
- Model-emitted `INFERRED` / `RECOMMENDED` findings are added, validated against the
  observed set. A model finding citing a `tool_call_id` that does not exist is rejected
  as `AGENT_GROUNDING_FAILURE` (§56) rather than silently displayed.

This is what makes §36's evidence model and §51's incident detail page real rather than
rendered from post-hoc reconstruction.

## Phase 7 — Revenue measurement + dashboard · M · ~3d

**Closes §49, §50, §51. Requires phases 1 and 4.**

- The six-way ledger: at risk / eligible / attempted / recovered / failed / unknown,
  computed deterministically from incidents, recovery actions and verification states.
- §49's rule is a hard constraint, not a display preference: the platform must never
  report the whole at-risk figure as recovered. The `unknown` bucket maps directly onto
  unsettled actions, so the existing UNKNOWN machinery feeds it — this is the payoff for
  having made UNKNOWN first-class.
- Dashboard (§50) and incident detail (§51) in the existing React SPA
  (`web/src/routes/`). `/metrics` today reports ops counters, not revenue; these are
  separate surfaces and should stay separate.

## Phase 8 — Taxonomy, versioning, observability · S–M · ~2d

**Closes §41, §47, §56, §58, §59.**

- Map `FailureCode` (`models.py:63`) onto §56's 18 categories; add `retryability` and
  `owning_subsystem`, which §56 requires on every failure and which nothing currently
  carries.
- Record `tool_registry_version`, `policy_version`, `workflow_version` on every run
  (§41). Agent/model/prompt versions are already recorded.
- Align audit event names to §47's list; promote `correlation_id` to a column on
  `audit_logs` rather than a payload key.
- Extend the SPA's trace view to the incident-rooted trace of §58.

---

## Order and dependencies

```text
0 ──> 1 ──> 2
      │     │
      │     └──> 7
      ├──> 3 ──> 4 ──> 5
      │          │
      │          └──> 7 ──> 8
      └──> 6 ────────────> 8
```

Recommended sequence: **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8**
(0 and 1 complete; 2 is next, and now also carries the `events` table deferred from 1.)

Rationale: 1 is the spine and gates the product framing. 2 is small, independently
valuable, and makes reconciliation evidence-driven. 3 precedes 4 and 5 because both
introduce actions that need a risk model. 6 is independent after 1 and can move earlier
if the §37 schema matters more than recovery. 7 is the merchant-visible payoff and needs
1 and 4 in place to have anything true to display.

Rough total: **22–26 working days**, single developer.

## Smallest credible slice

If the full sequence does not fit: **phases 0 + 1 + 2** (~7–8 days) convert this from an
investigate-and-refund agent into the real-time operations loop MerchantOps.md describes.
Everything after that deepens a system whose shape is already correct. Stopping before
phase 1 leaves the governing spec's central claim — §73's "not a chatbot connected to
Razorpay" — unsupported by the code.
