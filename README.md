# 🏦 MerchantOps Agent

[![CI](https://github.com/nivasthumma/Merchantagent/actions/workflows/ci.yml/badge.svg)](https://github.com/nivasthumma/Merchantagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-347%20passed-brightgreen.svg)](#-measured-results)
[![Scenarios](https://img.shields.io/badge/scenarios-167%2F167-brightgreen.svg)](#-measured-results)
[![Mutations caught](https://img.shields.io/badge/mutations%20caught-71%2F71-brightgreen.svg)](#-measured-results)

An AI agent that investigates merchant payment and revenue problems, recommends a
corrective action, and — only with human approval — executes it through a controlled
tool, then independently verifies what actually happened.

> **Independent developer project. Uses Razorpay Test Mode APIs where applicable.
> Not affiliated with, sponsored by, or endorsed by Razorpay.**

The point of this project is not the chatbot. It is the **trustworthy action loop
around the agent**:

```
DETECT → INCIDENT → REASON → DECIDE → POLICY CHECK → HUMAN APPROVAL → ACT
       → VERIFY → AUDIT → REPLAY → EVALUATE
```

The loop begins at detection, not at a question: a deterministic sweep over payment
history raises incidents, and an incident dispatches the agent. Asking a question
directly is the second entry point, not the only one.

---

## 📑 Contents

**Start here** — what is real, what is measured, and what it looks like running:

| | |
|---|---|
| [🧭 Built vs designed](#-built-vs-designed) | What ships today vs what is architecture |
| [⚠️ Two honesty disclosures](#-two-honesty-disclosures) | Mocked execution, and what the metrics measure |
| [📊 Measured results](#-measured-results) | 347 tests · 167/167 scenarios · 71/71 mutations |
| [▶️ Demo](#-demo) | Seven steps, end to end, in five minutes |

**How it works** — the machinery the project exists to demonstrate:

| | |
|---|---|
| [🏗️ Architecture](#-architecture) | The request path, gate by gate |
| [🔀 Synthetic / real boundary](#-data-architecture-the-synthetic--real-boundary) | Why the two data worlds never mix |
| [🔐 Security model](#-security-model) | Every control, where it lives, and its test |
| [🧪 Evaluation methodology](#-evaluation-methodology) | Grading behaviour, not prose |
| [🔁 Reconciliation](#-reconciliation) | Resolving `UNKNOWN` without re-issuing money |

**Run it** — and the parts that are honest about their edges:

| | |
|---|---|
| [⚙️ Setup](#-setup) · [🔌 API](#-api) | Local install; the endpoint surface |
| [🚧 Known limitations](#-known-limitations) | Split by *why* each one exists |
| [🗺️ Roadmap](#-roadmap) · [📁 Repository layout](#-repository-layout) | What is next; where things live |
| [📄 License / disclaimer](#-license--disclaimer) | MIT, and what this project is not |

---

## 🧭 Built vs designed

Read this table before anything else. It is the difference between what runs today
and what is architecture.

| Area | Built and running | Designed, not built |
|---|---|---|
| Detection | Deterministic sweep over payment history **and the provider event store**: success-rate degradation, duplicate capture, provider failure bursts. Idempotent, merchant-scoped | Internal event sourcing |
| Webhooks | Signed ingestion, event dedup, durable event store. A webhook triggers an independent read — it never writes state | Async queue; replay of stored events |
| Incidents | Full §13 lifecycle, evidence, computed revenue-at-risk, incident-rooted trace, reconciliation mismatches | — |
| Recovery | Deterministic planner: eligibility, attributed expected recovery, per-campaign budgets, stopping rules. Refunds and payment links execute; bulk campaigns escalate rather than run | RETRY and SUBSCRIPTION_RETRY; campaign-level approval |
| Agent | One bounded agent, **15 typed tools** (§18 complete) | Specialised multi-agent orchestration |
| Reasoning | Provider abstraction: Anthropic (`claude-opus-5`, adaptive thinking, prompt caching) **or** a deterministic planner. Credential detection covers all four SDK sources | Model routing, cost-aware selection |
| Policy | Deterministic engine: RBAC, merchant isolation, computed risk, amount limits, duplicate guard | Per-merchant configurable policy, approval chains |
| Approval | Server-side, expiring, re-checked at execution. **Dual approval** for CRITICAL risk, enforced by a UNIQUE constraint | N-of-M chains, delegation |
| Execution | Razorpay Test Mode adapter **or** deterministic mock (see below) | Production integration |
| Verification | Independent read-back with SUCCESS/FAILED/PARTIAL/UNKNOWN | — |
| UNKNOWN | First-class, **resolvable**; reconciliation sweep + escalation queue | Always-on worker (needs a queue) |
| Audit | Append-only **enforced by PostgreSQL**, secrets redacted, correlation-id traces (§58) | Distributed tracing |
| Replay | PLAYBACK + RE_REASON against frozen tools | Cross-version replay |
| Evaluation | 167 scenarios + 71-mutation validation, gated in CI; §42 promotion gate | Larger benchmark |
| Data | Seeded synthetic dataset, 2 merchants; durable provider-event store | Streaming / generated datasets |
| UI | Streamlit **and** a React SPA (`web/`): §49 recovery ledger, §50 dashboard, §51 incident page | Next.js, SSR |
| Infra | Local, PostgreSQL only | Redis / Celery / containers |

Nothing in the right column is claimed as implemented.

---

## ⚠️ Two honesty disclosures

These are stated up front rather than buried, because the project's entire premise
is that measured claims beat impressive ones.

**1. Payment execution is currently mocked.** No Razorpay credentials were available
in the build environment, so `scripts/razorpay_spike.py` returned verdict `mock`
(see `docs/assessment/razorpay-spike.md`). The mock adapter is a deterministic local
double. **Policy, approval, idempotency, verification and audit are identical on both
paths** — only the outbound HTTP call differs. Supply `RAZORPAY_KEY_ID` /
`RAZORPAY_KEY_SECRET`, re-run the spike, and the same code executes real Test Mode
refunds. The health endpoint and the UI both report which path is active.

**2. Reported metrics measure the harness, not model intelligence.** No Anthropic
credential of any kind is present here — not an API key, an auth token, an
`ant auth login` profile, or workload identity — so the agent runs on
`DeterministicProvider`, a rule-based planner rather than a language model. This is
deliberate: it makes the evaluation suite reproducible and isolates *what* is being
measured. A failing scenario is a defect in policy, verification, idempotency or
isolation — not model variance. Supply any of those credentials (or set
`LLM_PROVIDER=anthropic`) to run the same scenarios against `claude-opus-5`; those
numbers would measure something different and should be reported separately.
`/health` reports which credential source was found, so `deterministic` is never
ambiguous between "chosen" and "nothing was detected".

---

## 📊 Measured results

From `make eval` — actual execution, not targets:

```
167/167 scenarios passed      (critical: 110/110)

  adversarial_security  30/30    recovery              13/13
  detection              9/9     refund_policy         27/27
  duplicate_payment     16/16    revenue_investigation 18/18
  failure_unknown       20/20    risk_approval          7/7
  payment_failure       14/14    webhook                5/5

median task latency 39 ms · mean grounding rate 1.0
```

**A suite that passes everything proves nothing on its own.** `make mutants`
deliberately breaks each core control and re-runs the suite:

```
71/71 mutations caught
```

*Measured in one full run — 55 mutants, each re-running the whole scenario and test suite.*

That run is what makes the 167/167 meaningful — and it is how three real gaps
were found and closed (see below), plus a fourth in the detection engine: hour-bucket
onset had no volume floor, so ordinary variance was being reported as the moment a
degradation began.

Counted honestly, as before. Of the eighteen mutants added since, **sixteen produce a
graded scenario failure** — including all four webhook controls, both directions of the
risk floor rule, and every recovery bound. Two are caught by unit tests only: allowing any
incident lifecycle transition, and grading a bulk action as if it stood alone. No scenario
distinguishes either, for reasons given under coverage limits.

**Five real defects found by the harness, all invisible to a green suite.** A recovery mutant survived
because a clamp of mine forced §49's ordering to hold, making a wrong figure
indistinguishable from a right one. A tooling scenario turned out to be asserting nothing —
it checked that an unauthorised analyst did not reach a tool the planner never called for
anyone. And two output mutants survived their first run because the tests covering them
asserted the wrong layer: one checked that a task halted but never what the API told a
client, the other drove a helper directly so breaking its caller was invisible. That last
pattern has now appeared three times. And building the §49 ledger exposed two live defects
neither the suite nor the harness could have found, because until something reported recovery
there was nothing to contradict: a payment link that had merely been *sent* was counted as
the full charge recovered, and every recovery candidate was being dispatched as a refund
request whatever intervention had been planned. Both were mappings that were total when
written and became partial when Phase 5 added a case.

Configuration: `llm_provider=deterministic`, `payment_adapter=mock`,
`dataset=synthetic-v1 (seed 20260825)`. Counts are reported rather than percentages.
Verified reproducible: two consecutive runs produce an identical pass/fail vector.

Test suite: **347 passed** (`make test`) across unit, security and integration, in
under 5 seconds — the suite seeds once and rolls each test back, rather than rebuilding the
schema 200 times.

---

## ▶️ Demo

```bash
make seed && make demo
```

Seven steps, each printing what actually happened:

1. **"Why did revenue drop this week?"** — the agent calls `get_revenue_summary`,
   then `get_payment_metrics`, then drills into the worst method. It finds the
   planted cause: UPI success fell 91.8% → 73.2% while other methods held, with
   failures clustered at 18:00–20:00 and a dominant `UPI_COLLECT_TIMEOUT` error.
   **The cause is nowhere in the system prompt** — a test asserts this.
2. **Analyst attempts a refund** → `missing_permission`, DENY, no external call.
3. **Duplicate detected** → refund recommended → policy returns `REQUIRE_APPROVAL`
   → execution pauses. No external call has been made.
4. **Human approves** → policy re-checked server-side → refund executes →
   independent verification reads back the payment → `SUCCESS`.
5. **Audit trace** — every step, append-only.
6. **Replay** — both modes, zero external calls, refund count unchanged.
7. **UNKNOWN** — the refund lands but the response is lost. Reported `UNKNOWN`,
   never SUCCESS or FAILED. Re-verification reconciles it by idempotency key,
   recovers the reference, and settles it `SUCCESS` — with exactly one refund row.

---

## 🏗️ Architecture

```
    Synthetic dataset                  the analytical truth
    customers · orders · payments      (revenue is COMPUTED from
    failures · duplicate scenarios      payments — no revenue table)
                    │
                    ▼
    ┌───────────────────────────────┐
    │      PostgreSQL               │  business data
    │                               │  + execution state: agent_tasks,
    │                               │  tool_calls, agent_actions,
    │                               │  approvals, audit_logs, evaluations
    └───────────────┬───────────────┘
                    │  ▲ every stage below reads and writes here
                    ▼  │
                    ┌──────────────────┐
                    │   Streamlit UI   │
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │  Agent Runtime   │  bounded loop, budget-capped
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │ Typed Tool Layer │  6 tools, strict schemas
                    └────────┬─────────┘
                             ▼
        ┌────────────────────────────────────┐
        │  1. Argument validation            │──► TOOL_INVALID_ARGUMENT
        │  2. Policy engine                  │──► DENY      no external call
        │  3. Approval gate (HIGH risk)      │──► REJECT    no external call
        └────────┬───────────────────────────┘
                 │              ← the authorization authority
       ┌─────────┴─────────┐
       ▼                   ▼
  Synthetic DB      ┌─────────────────┐
  (read tools)      │  Mapping layer  │  SYN_PAY_xxxx → pay_xxxx
                    └────────┬────────┘  the ONLY synthetic→provider bridge
                             ▼
                     Razorpay adapter     Test Mode | mock
                             ▼
                    ┌──────────────────┐
                    │   Verification   │  reads the PAYMENT back,
                    │                  │  not the create response
                    └────────┬─────────┘
          ┌──────────┬───────┴───────┬──────────┐
          ▼          ▼               ▼          ▼
       SUCCESS    FAILED         PARTIAL    UNKNOWN
                                     │          │
                                     └────┬─────┘
                                          ▼
                                  Reconciliation      cron / on demand
                                  re-runs verification by idempotency key
                                          │
                              settled ────┴──── escalated to operator queue
                                          │
                                          ▼
                                   Audit  ·  Replay
```

The model requests; the deterministic application decides. It never sees a secret,
never constructs a URL, never picks its own merchant scope, and cannot override a
policy outcome.

Four details in that diagram are load-bearing:

- **Four terminal states, not three.** `PARTIAL` is reachable — a provider can accept
  a refund while the payment's `amount_refunded` never moves.
- **Verification precedes reconciliation.** Verification runs on every action;
  reconciliation is a bounded retry loop around it, entered only for `UNKNOWN` and
  `PARTIAL`.
- **The mapping layer is on the critical path.** It is the only route from a synthetic
  id to a provider id, so the agent can never name one.
- **There are no webhooks, deliberately.** A webhook is something you are *told*;
  verification reads state *back*. It would buy latency, not truth.

Full detail — the request path gate by gate, the verification predicate, and the
shape a webhook would take if one were added:
[`docs/architecture.md`](docs/architecture.md).

---

## 🔀 Data architecture: the synthetic / real boundary

This separation is mandatory and easy to get wrong.

```
Synthetic dataset  →  investigation + evaluation      (the analytical truth)
Razorpay Test Mode →  execution + state verification  (the action surface)
```

A test-mode account contains no organic revenue trend, no UPI failure pattern and no
naturally occurring duplicate payments. Treating it as an analytics source would be
dishonest. The two worlds are joined by an explicit **mapping layer**: 21 synthetic
payments carry an `external_payment_id`, and the action layer resolves synthetic → external
through it. **The agent can never invent a provider id.**

Dataset (`seed 20260825`, byte-identical every run): 2 merchants, 200 customers,
30 products, 581 orders, 589 payments, 20 refunds, 21 externally mapped payments,
6 prompt-injection sites.

---

## 🔐 Security model

| Control | Where it lives | Test |
|---|---|---|
| Authorization outside the model | `app/policy/engine.py` | `test_unauthorized_user_cannot_refund` |
| Merchant isolation | SQL predicate + policy gate | `test_cross_merchant_*` |
| Untrusted data tagging | `Evidence.untrusted` + `<untrusted_merchant_data>` delimiters | `test_injected_text_is_tagged_untrusted` |
| Injection resistance | asserted at the **policy layer**, not on prose | `test_injection_in_customer_notes_does_not_cause_refund` |
| Idempotency | server-derived key + `UNIQUE` constraint | `test_double_approval_produces_one_refund` |
| Argument validation | before policy touches the database | `test_malformed_arguments_rejected_*` |
| Secret redaction | `app/audit/trace.py` | `test_secrets_are_redacted_from_traces` |
| Loop budget | 12 tool calls / 8 turns / 60s | `test_budget_terminates_runaway_loop` |

The injection claim is deliberately narrow: **"no external call occurred and the
decision was recorded"** — not "the agent resisted". Threat model:
[`docs/threat-model.md`](docs/threat-model.md).

---

## ⚙️ Setup

Requires Python 3.12+ and PostgreSQL.

```bash
createdb merchantops                     # or use the DATABASE_URL of your choice
cp .env.example .env                     # optional; defaults work locally
make setup                               # venv + dependencies
make seed                                # deterministic dataset
make test                                # 81 tests
make eval                                # 106 scenarios, measured
make mutants                             # prove the suite catches regressions
make harden                              # enforce audit immutability, then verify it
make ci                                  # what CI runs: seed + harden + test + eval
make demo                                # full end-to-end walkthrough
```

Run the services:

```bash
make api      # FastAPI on :8000  (docs at /docs)
make ui       # Streamlit on :8501
```

Or the React SPA, which talks to the same API:

```bash
make web-setup                    # npm install (once)
make api                          # in one terminal
make web                          # Vite dev server on :5173
make token USER_ID=USR_A_OWNER    # paste the token into the app
```

```bash
make web-test                     # 39 Vitest tests
```

The SPA is outside the contract's MVP scope (§3, §52) and exists by explicit request —
see [ADR-0015](docs/adr/0015-react-spa-frontend.md). The Streamlit UI remains the
contract-conformant surface. The SPA's tests are not in CI, so they are a local gate
rather than a regression gate.

Before trusting real payment execution:

```bash
make spike    # writes docs/assessment/razorpay-spike.md
```

---

## 🔌 API

| Endpoint | Purpose |
|---|---|
| `GET /metrics/operational` · `GET /metrics/objectives` | §59 metrics and §60 SLOs |
| `GET /approvals` · `GET /actions/{id}` | The approval queue, and one action |
| `GET /tasks/{id}/messages` | The conversation the model actually saw |
| `GET /trace/{correlation_id}` | §58 — everything one operation touched, in one ordering |
| `GET /failures/taxonomy` | §56/§57 — what each failure means and whether to retry it |
| `GET /dashboard` | §50 — revenue at risk, recovery, incidents, agent activity |
| `GET /recovery/ledger` | §49 — the six figures, and whether they nest |
| `POST /incidents/{id}/recovery` | Plan recovery — candidates, expected value, budget |
| `GET /recovery/plans/{id}` | Plan detail with ranked candidates |
| `POST /recovery/candidates/{id}/dispatch` | Act on one candidate, bounds permitting |
| `POST /recovery/plans/{id}/settle` | Read outcomes back from verified actions |
| `POST /webhooks/razorpay` | Provider event ingestion — **HMAC-signed, unauthenticated** |
| `GET /webhooks/events` | The durable event store, merchant-scoped |
| `POST /incidents/detect` | Run the detection sweep (idempotent) |
| `GET /incidents` | Open incidents, ordered by revenue at risk |
| `GET /incidents/{id}` | Detail: signals, evidence, tasks, legal next states |
| `GET /incidents/{id}/trace` | Detection, every lifecycle move, every task event |
| `POST /incidents/{id}/investigate` | Dispatch the agent against an incident |
| `POST /tasks` | Create an agent task |
| `GET /tasks/{id}` | Task status, approvals, actions |
| `GET /tasks/{id}/trace` | Full audit trace |
| `POST /tasks/{id}/approve` | Approve and execute a pending action |
| `POST /tasks/{id}/reject` | Reject; no external call |
| `POST /tasks/{id}/reverify` | **Resolve an UNKNOWN action** |
| `POST /tasks/{id}/replay?mode=` | `PLAYBACK` or `RE_REASON` |
| `POST /actions/reconcile` | Settle unsettled actions (re-reads only) |
| `GET /actions/escalated` | Operator queue: what reconciliation could not settle |
| `GET /scenarios` · `POST /scenarios/{id}/run` | Evaluation suite |
| `GET /health` | Reports active LLM provider and payment adapter |

Every endpoint enforces authentication and merchant isolation server-side. A
cross-merchant read returns 404, not 403 — existence is not leaked.

`POST /webhooks/razorpay` is the one exception and the only unauthenticated write: the
provider holds no bearer token, so an HMAC signature over the raw body is the
authentication. It returns 200 once the delivery is stored — including for a signature
that failed, because a non-2xx only makes the provider retry a forgery. What actually
happened is in the response body and in `webhook_events`.

---

## 🧪 Evaluation methodology

Scenarios grade **observable behaviour**, never prose: tool sequence, arguments,
policy decision, approval requirement, final status, verification state, evidence
grounding, and whether an external financial effect occurred.

"Deterministic" here means *the same scenario state produces a reproducible
evaluation of observable behaviour* — not identical wording. Each scenario runs
against a freshly seeded database so scenarios cannot contaminate one another.

**Evidence grounding is mechanical, not judged.** Every material claim is a typed
`Finding{claim, kind, evidence_refs}`; an `OBSERVED` claim must cite a resolvable
`tool_call_id`. Grounding rate = grounded OBSERVED findings ÷ total OBSERVED findings.
No LLM judge, no rubric.

Details: [`docs/evaluation.md`](docs/evaluation.md).

---

## 🔁 Reconciliation

`UNKNOWN` is a pending safety state, and a pending state that nobody resolves is not
safety — it is deferral. The sweep closes that gap:

```bash
make reconcile                       # or: .venv/bin/python scripts/reconcile.py
*/5 * * * * cd /path && .venv/bin/python scripts/reconcile.py   # from cron
```

It **never retries the action**. It re-reads external state, reconciling by the
action's own idempotency key — the only way to learn whether a lost-response refund
actually landed. A blind retry of a financial action with an unknown outcome is the
most dangerous thing this system could do, and the sweep cannot perform one.

Three properties make it safe to run unattended:

- **Min-age guard** — actions younger than 30s are skipped. A refund submitted
  seconds ago may simply not have propagated; burning an attempt on it can escalate a
  healthy action.
- **Bounded attempts** — after 5 tries an action is *escalated*, not swept forever. It
  appears in `GET /actions/escalated` and in the UI sidebar, and the CLI exits `2` so
  a cron wrapper can alert.
- **Settlement is a read** — verified by `test_sweep_settles_unknown_without_reissuing`,
  which asserts the refund row count is unchanged.

## 🚧 Known limitations

These are split by *why* they exist, because "we chose not to" and "we could not"
are different claims.

### Blocked on credentials — cannot be closed here

1. **Payment execution is mocked in this build.** No Razorpay credentials were
   available; `make spike` returned verdict `mock`. Supply credentials and the same
   code path executes real Test Mode refunds.
2. **Reasoning is a deterministic planner.** No Anthropic credential is present in
   any form the SDK accepts. Published metrics therefore measure the control plane,
   not agent intelligence. A consequence worth stating plainly: **the Anthropic
   provider has never executed against the API in this build.** Its wire translation
   and prompt caching are unit-tested, not end-to-end verified — and writing those
   tests is what surfaced a request-shaping bug that would have failed every
   tool-using task on its second turn (ADR-0014).
3. **`RE_REASON` replay consistency is untested against a real model.** With the
   deterministic planner it is trivially 1.0, so it is not published as a meaningful
   number. Against `claude-opus-5` it would be a genuine measurement.

### Deliberate scope decisions

4. **Reconciliation is a sweep plus webhooks, still not a daemon.** A signed provider
   event now settles an action the moment it arrives, so the common path is no longer
   sweep-cadence. The sweep remains the backstop for actions no webhook ever arrives
   for — a lost delivery, an event type the provider does not send — and that path is
   still bounded by cron, not real time. An always-on worker means Redis or Celery,
   which the MVP scope excludes.
5. **Single-process, synchronous.** No queue, no horizontal scale.
6. **Rate limiting is per-worker.** The counter is in-process, so with several
   workers the limit is approximate. A shared counter needs Redis.
7. **`RETRY` and `SUBSCRIPTION_RETRY` have no tool.** They are planned, ranked and costed
   but cannot be carried out; dispatch refuses them as `not_executable`. `REFUND` and
   `PAYMENT_LINK` execute.
8. **`send_customer_notification` runs only against the mock adapter.** Razorpay notifies
   *about a payment link*; it is not a messaging service, and no email or SMS provider is
   configured. The live adapter fails closed with `INTEGRATION_UNAVAILABLE` rather than
   reporting a contact that never happened.
9. **`CUSTOMER_NOTIFICATION` is not planned as a standalone intervention.** No incident
   type maps to it — it is something reached for alongside a recovery, not a recovery in
   itself. The tool exists; the planner never proposes it.
10. **§16's temperature is not set, and cannot be.** Sampling parameters were removed on
    the Claude Opus 5 family — sending `temperature` returns a 400 — so the spec's
    "temperature: 0" is not implementable. `output_config.effort` is the control that
    replaced it; the deviation is stated in the provider and asserted by a test.
11. **Three §59 metrics are reported as unavailable, not estimated.** Root-cause accuracy
    and revenue-at-risk accuracy need labelled ground truth a production incident does not
    carry; agent cost needs token accounting this build has no path to. A figure computed
    from nothing is worse than a blank.
12. **The provider-burst detection rule cannot fire on the seeded dataset.** It reads
    `webhook_events`, and the seed is payment history — it carries no provider events. The
    rule is real and is exercised by constructed state in tests and in `CLS-01`/`CLS-02`,
    the same honesty as the bulk-risk path.
13. **`confidence` is a display value.** It is recorded and shown and consulted by nothing.
    Against the deterministic planner it is computed from evidence count, which measures
    the planner rather than any judgement; against a real model it would mean something
    different and should be reported separately.
14. **Authentication is HMAC bearer tokens, not an identity provider.** Tokens are
   unforgeable and permissions are read from the database on every request, but
   there is no expiry, rotation, revocation list, or audience binding.
16. **Detection observes state, not a stream.** `webhook_events` stores what the provider
   *tells* us, but the detection rules still read `payments`. A business change that
   never lands on a payment row is invisible to them. Wiring detection onto the event
   store is real work, not a rename.
16. **Detection is a sweep, not a daemon** — same trade-off as reconciliation, above.
    Incidents appear at sweep cadence.
17. **Only 21 of 589 payments are externally mapped.** Refunds outside that set are
   correctly rejected as `not_externally_mapped` — that is the mapping layer working,
   not a defect.

### Coverage limits

15. **Thirteen of the 45 mutants are caught by unit tests only** — no scenario
    distinguishes them: idempotency-key derivation, the duplicate-action SAVEPOINT,
    the key-name branch of audit redaction, the incident lifecycle's legality check, and
    grading a bulk action as if it stood alone, and six of the seven tooling controls.
    The tooling ones are structural: the deterministic planner does not compose customer
    contact on its own, so no scenario can drive most of those paths, and giving it that
    freedom would be the wrong fix. Each is reachable in principle but sits behind a guard that fires first in
    every path a scenario can drive — for the lifecycle mutant, because every
    transition a scenario can drive is already a legal one. Two more (registry lookup,
    argument validation) are detected as a *crash* rather than a graded failure — the
    suite dies on `spec is None` instead of reporting SEC-24 red.
    Counted honestly: **40 of 69 produce a graded scenario failure**, 4 are detected as a
    crash rather than a graded result, and the rest by unit tests alone — the metrics and
    taxonomy ones are read-side aggregates the scenario suite has no way to drive. See
    [`docs/evaluation.md`](docs/evaluation.md) for the per-mutant breakdown.
16. **The 45-mutant run is slow.** Each mutant re-runs the full scenario and test suites.
    The test half is now fast (one seed, per-test rollback); the scenario half still
    rebuilds the schema per scenario, so a complete run is around fifty minutes.
    `scripts/mutation_test.py <substring>` runs a subset during development; CI runs
    all of them.

---

## 🗺️ Roadmap

The governing specification is now [`docs/MerchantOps.md`](docs/MerchantOps.md), which
supersedes `docs/CONTRACT.md` (see [ADR-0016](docs/adr/0016-merchantops-spec-supersedes-contract.md)
for the §-number crosswalk). The ordered plan to close the distance between the two is
[`docs/gap-closure-plan.md`](docs/gap-closure-plan.md); phases 0 and 1 are delivered.

Next, in order:

1. **A computed risk engine** (§24) — risk derived from value, reversibility and bulk
   size rather than a static per-tool constant, with `CRITICAL` and dual approval.
3. **Recovery planner, budgets and stopping rules** (§23, §27, §28).
4. The remaining nine tools of §18; model-emitted structured output (§37); the
   revenue-recovery ledger and dashboard (§49, §50).

Independent of the plan, and still blocked on credentials:

- Complete the Razorpay Test Mode spike with real credentials; map genuine captured
  payments; publish a second results table for real execution.
- Run the suite against `claude-opus-5` and publish model-vs-harness results side
  by side, including replay consistency.

Done: ~~background reconciliation for `UNKNOWN` actions~~ (sweep + escalation queue);
~~expand to 100 scenarios and wire into CI~~ (115 scenarios, mutation testing, GitHub
Actions gate); ~~detection and incident management~~ (ADR-0017); ~~webhook ingestion and
the durable event store~~ (ADR-0018); ~~computed risk, `CRITICAL` and dual approval~~
(ADR-0019); ~~recovery planning, budgets and stopping rules~~ (ADR-0020); ~~the fifteen tools of §18~~ (ADR-0021); ~~the §37 agent output schema~~ (ADR-0022); ~~the §49 recovery ledger and §50/§51 pages~~ (ADR-0023); ~~failure taxonomy, versioning and traces~~ (ADR-0024).

---

## 📁 Repository layout

```
app/
  webhooks/     signed ingestion, dedup, the durable provider-event store
  recovery/     planner, per-campaign budgets, stopping rules, dispatch, §49 ledger
  detection/    deterministic rules + the idempotent incident sweep
  incidents/    §13 lifecycle state machine, investigation dispatch
  agent/        runtime (bounded loop), approval, replay, versioned prompts
  tools/        typed registry, contracts, investigation + action tools
  policy/       deterministic policy engine + the computed risk engine
  failures.py   §56 taxonomy and §57 retry rules, as data
  metrics.py    §59 operational metrics and §60 objectives
  verification/ read-back verification and state classification
  integrations/ razorpay adapter + fault-injection seam
  llm/          provider abstraction (anthropic | deterministic)
  eval/         scenario schema + runner
  api/          FastAPI surface
ui/             Streamlit app
web/            React SPA — Vite + TypeScript (ADR-0015), 168 tests
data/           167 scenarios + the last evaluation report
scripts/        seed, spike, scenarios, demo
tests/          unit · security · integration  (347 tests)
docs/           MerchantOps.md (governing spec), CONTRACT.md (superseded),
                architecture (+ assumptions), threat model, evaluation,
                gap-closure plan, 28 ADRs
```

## 📄 License / disclaimer

Licensed under the MIT License — see [`LICENSE`](LICENSE).

Independent developer project, provided as-is for demonstration purposes. Uses
Razorpay Test Mode APIs where applicable. Not affiliated with, sponsored by, or
endorsed by Razorpay. No real-money transactions are performed anywhere in this
codebase.

Security reports: [`SECURITY.md`](SECURITY.md).
