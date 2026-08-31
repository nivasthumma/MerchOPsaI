# Gap-closure plan — CONTRACT.md → MerchantOps.md

**Status:** all eight phases delivered — **not** the same as every spec section closed

> Eight phases done is a statement about this plan, not about `MerchantOps.md`. A
> section-by-section audit is in [`docs/spec-coverage.md`](spec-coverage.md): the
> architecture is closed, the instrumentation is not — most of §59's metrics, one §60 SLO,
> §16's temperature, two §65 routes, seven §66 tables and the `tenant_id` dimension of §11
> and §54 all remain open.
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

## Phase 2 — Webhook ingestion · M · ~2–3d — **DONE**

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

**Delivered** — see [ADR-0018](adr/0018-webhooks-as-evidence.md). `app/webhooks/`
(ingestion + processing), the `webhook_events` durable store deferred from phase 1, two
routes, `RECONCILIATION_MISMATCH` incidents, 5 scenarios, 4 mutants, 15 tests. The store
holds MerchantOps §11's field list; it carries provider-delivered events only, and
detection still reads `payments` rather than the event log.

## Phase 3 — Risk engine + policy expansion · M · ~2d — **DONE**

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

**Delivered** — see [ADR-0019](adr/0019-computed-risk-and-dual-approval.md).
`app/policy/risk.py`, `approval_signatures` with `UNIQUE(approval_id, user_id)`, the
registry as sole declaration of risk and permissions, 7 scenarios, 4 mutants, 35 tests.

**One correction to this plan.** It implied computed risk should be able to reach CRITICAL
on value. §24's worked example grades a ₹5,000 refund — merchant A's entire limit — as
HIGH and reserves CRITICAL for *bulk*. Letting value reach CRITICAL made the seeded demo
refund require two approvers and broke nineteen tests; the tests were right. Value now
caps at HIGH, and the one path to CRITICAL is a further action on a payment whose previous
action never settled. Bulk arrives with phase 4, which is what creates bulk actions.

## Phase 4 — Recovery planner, budget, stopping rules · L · ~4d — **DONE**

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

**Delivered** — see [ADR-0020](adr/0020-recovery-planning-budgets-and-stopping.md).
`app/recovery/`, `recovery_plans` + `recovery_candidates`, `customers.contact_opted_out`,
four routes, 8 scenarios, 5 mutants, 27 tests. Bulk size is now a real risk input, closing
the ADR-0019 deferral.

**Two things found on the way.** Phase 1's duplicate detection emitted one incident per
*pair*, so an in-window triple would have claimed 3x a 2x exposure — a latent revenue
overcount, now one incident per order. And a recovery mutant survived: a clamp added to fix
a rounding drift was forcing §49's ordering to hold, making a wrong figure
indistinguishable from a right one. Rounding the aggregate once fixed both.

## Phase 5 — Tool registry expansion · M · ~3d — **DONE**

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

**Delivered** — see [ADR-0021](adr/0021-the-fifteen-tools.md). Nine tools, the
read/action partition enforced by test, `action:recover` split from `action:refund`,
`payment_links` and `notifications` provider tables, 10 scenarios, 7 mutants, 34 tests.
`PAYMENT_LINK` is now executable, which makes the bulk risk path reachable from the seeded
dataset and closes the ADR-0020 limitation.

**Two things the trap warning did not cover.** A policy control was firing for the wrong
reason: the refund amount limit ran for any tool carrying `amount_minor`, so a payment link
was measured against the merchant's refund limit — invisible while only one money-shaped
tool existed. And extending the deterministic planner to reach the new tools broke the
recovery dispatcher, because a request naming an incident id was pulled into a read. A
request that asks for an action is never a lookup, whatever entities it mentions.

## Phase 6 — Agent output schema · M · ~2d — **DONE**

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

**Delivered** — see [ADR-0022](adr/0022-the-agent-output-schema.md). `app/agent/output.py`,
`E<n>` evidence labels running across a task, prompt v2 carrying §20's context contract,
`agent_confidence` and `model_requires_human`, 5 scenarios, 5 mutants, 22 tests.

**What the plan did not say, and should have.** The model's `requires_human` needed the same
floor rule as risk: it may raise the bar, never lower it. A mutant handing that field to the
model survived the first run, because the test covering it asserted the task halted and never
asserted what the API told a client. A second mutant, resetting evidence numbering per tool
call, also survived — the test drove the renderer directly, so breaking its caller was
invisible. Both gaps were tests aimed at the wrong layer rather than tests that were missing.

## Phase 7 — Revenue measurement + dashboard · M · ~3d — **DONE**

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

**Delivered** — see [ADR-0023](adr/0023-the-recovery-ledger.md). `app/recovery/ledger.py`,
`attributed_amount_minor` on candidates, two routes, `/dashboard` and `/incidents/:id` in the
SPA, 5 scenarios, 5 mutants, 13 backend tests, 13 web tests.

**Two live defects that building the report exposed**, neither findable before it because
nothing reported recovery to contradict. A payment link that had merely been *sent* was
counted as the full charge recovered — precisely what §49 forbids. And every recovery
candidate was dispatched as a *refund* request whatever intervention had been planned, so a
payment-link candidate was refused for being an unrefundable payment: safe, and for the wrong
reason. Both were mappings that were total when written and became partial when phase 5 added
an executable intervention, and neither had a test because at the time there was nothing to
distinguish.

## Phase 8 — Taxonomy, versioning, observability · S–M · ~2d — **DONE**

**Closes §41, §47, §56, §58, §59.**

- Map `FailureCode` (`models.py:63`) onto §56's 18 categories; add `retryability` and
  `owning_subsystem`, which §56 requires on every failure and which nothing currently
  carries.
- Record `tool_registry_version`, `policy_version`, `workflow_version` on every run
  (§41). Agent/model/prompt versions are already recorded.
- Align audit event names to §47's list; promote `correlation_id` to a column on
  `audit_logs` rather than a payload key.
- Extend the SPA's trace view to the incident-rooted trace of §58.

**Delivered** — see [ADR-0024](adr/0024-failure-taxonomy-versioning-and-traces.md).
`app/failures.py` (§56 categories + §57 retryability as data), `correlation_id` as an audit
column, `tool_registry_version` derived from the registry, `/trace/{correlation_id}` and
`/failures/taxonomy`, 5 scenarios, 5 mutants, 19 tests.

**One thing worth stating plainly:** nothing in the runtime branches on `may_retry()` yet.
The reconciliation sweep and the webhook path each independently implement the same rule the
table now encodes. The table is correct and published and tested; wiring the runtime to
consume it instead of restating it is real work and is not this phase.

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

Recommended sequence: **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8** — all delivered.

## What the plan got wrong

Kept because a plan that is only ever right in hindsight teaches nothing.

- **Phase 3's calibration.** The plan implied computed risk should reach CRITICAL on value.
  §24 grades a ₹5,000 refund — a whole merchant limit — as HIGH and reserves CRITICAL for
  bulk. Getting it wrong broke nineteen tests, and the tests were right.
- **Phase 1's `events` table.** Listed as phase 1 work; deferred to phase 2 because a table
  with no writer is the skeleton component both specs warn against.
- **Phase 5's trap warning was incomplete.** It named the read/action split, which was real.
  It did not anticipate that a policy control was firing for the wrong reason, or that
  extending the planner would break the recovery dispatcher.
- **Phase 4 and 7 found defects the plan could not have predicted**, because they only became
  visible once something measured them: a duplicate overcount, a payment link counted as
  money recovered, every candidate dispatched as a refund.

The pattern across all of them: **a mapping that was total when written and became partial
when a case was added.** Worth watching for in phase 9, whatever it turns out to be.

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
