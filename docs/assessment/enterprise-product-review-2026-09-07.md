# MerchantOps Enterprise Product + Architecture Review

Date: 2026-09-07
Repository: `nivasthumma/MerchOPsaI`
Review basis: `master` plus the enterprise UI improvement branch.

## Executive verdict

MerchantOps is materially stronger than a normal buildathon prototype. The repository already demonstrates a serious control-plane philosophy: deterministic detection, typed agent tools, policy/approval gates, verification, audit, replay, tenant isolation, RLS, token lifecycle, SCIM/OIDC foundations, synthetic/real separation, evaluation scenarios, and explicit disclosures about mocked payment execution and deterministic reasoning.

The largest remaining risk is not "missing enterprise architecture." It is **product truth at runtime**: the public experience still needs to make the system feel continuously operational, make live agent reasoning visible without pretending that prose is authoritative, make approvals/recovery the primary operator workflow, and prove real Test Mode execution rather than only carrying the adapter abstraction.

## Current scorecard

| Area | Assessment | Priority |
|---|---|---|
| Security / authorization | Strong foundation | P0 validate continuously |
| Financial safety | Strong control-plane design | P0 real provider spike |
| Evaluation | Excellent for prototype stage | P0 re-measure merged tree |
| Agent architecture | Strong bounded runtime | P0 real LLM integration |
| Detection / incidents | Good foundation | P1 continuous event-driven UX |
| Recovery UX | Functional but not operator-first | P0 |
| Real-time UX | Timeline exists; dashboard/queues were refresh-on-navigation | P0/P1 |
| UI visual system | Deliberate, accessible, trustworthy | P1 |
| UI information architecture | Too report-oriented in places | P0 |
| Production deployment | Docker/worker model exists | P0 deployment topology |
| Razorpay integration | Adapter exists; execution disclosed as mock | P0 |
| Enterprise scale | Designed more than proven | P1/P2 |

## What is already excellent

1. The README is unusually honest about built versus designed capabilities. It explicitly states that payment execution is currently mocked and that published evaluation numbers use the deterministic provider when no Anthropic credential is present.
2. The recovery ledger correctly distinguishes at-risk, recoverable, attempted, recovered, failed, unknown and outstanding amounts instead of presenting one misleading "recovered" KPI.
3. The UI and API types model `UNKNOWN` as a first-class verification state.
4. The task model records agent/model/prompt/tool/policy/workflow versions, enabling reproducibility.
5. The API client treats credentials as identity only and relies on the server for authorization.
6. The frontend has strong accessibility and trust-oriented details: skip navigation, reduced-motion-aware design, quarantined untrusted evidence, explicit shared-demo state, and visible execution/model disclosures.
7. The live timeline uses an event cursor rather than refetching the entire history and exposes pending outbox frames so a stalled drain is distinguishable from a quiet system.

## Highest-risk gaps

### P0 — Prove real provider execution

The repository itself states that payment execution is currently mocked. This is the largest credibility gap for a revenue-recovery product. Complete the Razorpay Test Mode spike with one captured payment, one bounded refund, provider read-back, webhook ingestion, reconciliation, and audit evidence. The UI should show a provider transaction reference and verification source.

### P0 — Replace navigation-driven operations with an operational command center

The dashboard and incident queue previously loaded once on mount. An operator needs to know that the queue and money figures are fresh. This branch adds a bounded foreground refresh hook to Dashboard and Incidents, pausing in hidden tabs and showing a live/paused/freshness state. Keep Timeline as the detailed event stream.

### P0 — Make approval/recovery the primary work queue

A merchant operator should not have to discover an approval inside a task detail page. Add a first-class **Action Center** containing:
- Awaiting approval
- Expiring approvals
- Executing actions
- UNKNOWN verifications
- Escalated actions
- Recently completed actions

Each row should expose amount, action, target, risk, policy decision, expiry, evidence readiness, and one primary next action.

### P0 — Make the agent visibly real

When Anthropic is configured, the UI must show model/provider, turn count, tool sequence, latency, evidence citations, policy decisions, and final recommendation. Do not render hidden chain-of-thought. Render observable tool activity and concise decision summaries instead.

The deterministic planner must remain clearly labeled as deterministic. The README's disclosure should remain synchronized with `/health`.

### P0 — Evaluation must be re-run on the exact merged tree

The README says 187/187 scenarios pass but also says the 113-mutant figure is not re-measured after the latest merges. Before presenting production-grade claims, run the full test/evaluation/mutation suite on the exact commit submitted. Publish the commit SHA and run timestamp alongside the results.

## Product improvements

### 1. Change the navigation model

Recommended primary navigation:

- **Command Center** — what needs attention now
- **Incidents** — operational problems
- **Actions** — money-moving work
- **Investigations** — agent work
- **Timeline** — system activity
- **Recovery** — financial outcome
- **Scenarios / Eval** — engineering trust surface
- **Administration** — identity, policies, integrations

Current `Investigate` as the first tab is useful for demos, but the product should open on the state of the merchant, not a blank prompt.

### 2. Command Center layout

Top row:
- Revenue health
- Revenue at risk
- Recoverable
- Recovered
- UNKNOWN exposure
- Open critical incidents

Middle:
- Critical incident queue
- Action Center
- Payment health by method

Bottom:
- Recovery funnel
- Recent system activity
- Agent health / provider state

Persistent header:
- Merchant selector
- Environment: Test/Live
- Provider connection state
- LLM state
- Last data update
- User/role

### 3. Incident detail should become a decision workspace

Recommended hierarchy:

1. What happened
2. Business impact
3. Why the system believes it happened
4. Evidence
5. Recommended intervention
6. Policy decision
7. Approval
8. Execution
9. Verification
10. Outcome
11. Audit / replay

The current incident page already contains most of these data, but they are rendered as sequential sections. Turn them into a structured decision workspace with a visible state machine and a single primary action.

### 4. Recovery needs a financial funnel

Show:

`At risk → Eligible → Planned → Approved → Attempted → Confirmed → Failed → UNKNOWN`

Every amount should be clickable to the underlying candidates. Expected recovery must never visually resemble confirmed recovery.

### 5. UNKNOWN needs its own queue

UNKNOWN is not an error badge. It is an unresolved financial state. Give it:
- age
- amount
- provider reference
- verification attempts
- last provider read
- next retry time
- escalation status
- reconcile button

## UI / UX improvements

### Trustworthy visual language

Keep the existing restrained palette. Do not introduce dashboard gradients, giant neon KPI cards, or decorative AI animations. Financial operations require visual hierarchy without ambiguity.

Improve:
- stronger primary-action hierarchy
- clearer status chips
- compact but richer table rows
- hover/focus affordances
- sticky action bar on incident detail
- responsive two-column evidence/action layout
- consistent empty/loading/error states
- explicit stale-data state

### Agent activity visualization

Use a compact vertical activity rail:

`Agent started → tool called → evidence found → hypothesis → policy → approval → action → verification`

Each step can expand to show input/output metadata, duration and evidence references. Never expose hidden reasoning or secrets.

### Approval UX

Approval card should answer five questions immediately:

1. What will happen?
2. To whom/which payment?
3. How much money?
4. Why is this allowed?
5. What evidence supports it?

Then show Approve / Reject with expiry and required approval count.

### Freshness UX

Every operational screen should show:
- Live / Paused
- Last updated
- Data source
- Environment

If refresh fails while old data is visible, show a stale warning instead of replacing the page with an error.

## Architecture improvements still required

1. **Provider execution:** complete real Razorpay Test Mode path and webhook verification.
2. **Durable worker:** move reconciliation/detection/notification/event drain to a managed worker in deployment rather than relying on manual or single-process cadence.
3. **Event delivery:** retain cursor semantics; add backpressure metrics, consumer lag, dead-letter/error accounting, and bounded retention.
4. **Idempotency:** require idempotency keys at every financial command boundary and expose them in audit metadata.
5. **Outbox reliability:** add explicit delivery attempts, last error, next attempt, and dead-letter state to the operational UI.
6. **Observability:** add OpenTelemetry traces with correlation IDs spanning HTTP → task → tool → policy → action → provider → webhook → verification.
7. **Rate/cost controls:** add per-merchant agent budgets for tokens, tool calls, wall-clock time and financial exposure.
8. **LLM governance:** persist provider/model/prompt/tool-registry/policy versions on every run; add model/prompt promotion gates to CI.
9. **Data retention:** define retention classes for agent messages, raw provider payloads, audit records, evidence and synthetic data.
10. **Disaster recovery:** test database restore, outbox recovery and replay against a restored database, not only unit mocks.
11. **Deployment topology:** if Vercel is used for the UI, deploy the Python API and worker to infrastructure that supports long-running processes. Do not place the scheduler/worker responsibility inside a serverless request lifecycle.
12. **Tenant isolation:** keep application checks plus RLS, and add tests proving background jobs cannot accidentally execute with a missing principal/tenant context.

## Code-quality improvements

- Split large agent runtime modules into bounded components: orchestration, model adapter, tool execution, policy handoff, persistence and output validation.
- Keep API response models generated/contract-checked as currently intended.
- Add frontend component-level accessibility tests for action dialogs, live regions, keyboard navigation and focus management.
- Add browser E2E tests for the five critical financial paths: deny, approval, execute, UNKNOWN, reconcile.
- Add performance tests for incident list, dashboard, event cursor and action center at realistic merchant volumes.
- Add migration smoke tests from a prior release to current release.
- Make CI report separate gates for unit, security, contract, evaluation, mutation and browser E2E.

## Enterprise readiness gates

Do not call the system production-grade until all of the following are true:

- Real Razorpay Test Mode transaction completed end to end.
- Provider webhook signature and duplicate handling demonstrated.
- Refund/action idempotency demonstrated under retry/concurrency.
- UNKNOWN reconciliation demonstrated.
- RLS cross-tenant test passes.
- Approval expiry/revocation race test passes.
- Agent prompt injection scenario passes.
- Real LLM run recorded with version metadata.
- Full evaluation and mutation run completed on the submission commit.
- Browser E2E covers the financial state machine.
- Dashboard, incidents and actions visibly refresh without manual navigation.
- Production deployment separates web/API/worker responsibilities.

## Implementation sequence

### Sprint 1 — Operator experience

- Merge live refresh hook for Dashboard/Incidents.
- Build Action Center.
- Redesign incident detail as a decision workspace.
- Add UNKNOWN queue.
- Add freshness/environment/provider badges.

### Sprint 2 — Real execution

- Razorpay Test Mode spike.
- Provider mapping verification.
- Webhook ingestion.
- Reconciliation.
- Failure injection tests.

### Sprint 3 — Agent productization

- Real LLM provider.
- Observable tool activity.
- Evidence-first answer cards.
- Agent budget/cost controls.
- Prompt/model governance.

### Sprint 4 — Production hardening

- Durable worker deployment.
- OpenTelemetry.
- Browser E2E.
- Load testing.
- Migration/restore tests.
- Full evaluation/mutation rerun.

## Product principle

The product should make one promise visually and technically:

> **The agent may reason broadly, but it may act only inside a narrow, observable and reversible control system.**

The architecture already approaches this principle. The next stage is to make that truth unmistakable in the operator experience and prove the external money-moving path with real Test Mode execution.
