# 🏦 MerchantOps Agent

[![CI](https://github.com/nivasthumma/MerchOPsaI/actions/workflows/ci.yml/badge.svg)](https://github.com/nivasthumma/MerchOPsaI/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-626%20passed-brightgreen.svg)](#-measured-results)
[![Scenarios](https://img.shields.io/badge/scenarios-167%2F167-brightgreen.svg)](#-measured-results)
[![Mutations caught](https://img.shields.io/badge/mutations%20caught-77%2F78-yellow.svg)](#-measured-results)

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
| [📊 Measured results](#-measured-results) | 626 tests · 167/167 scenarios · 88/88 mutations |
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
| [🚨 Runbook](docs/runbook.md) | Health, the `UNKNOWN` queue, deploys, restore, triage |
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
| Observability | Structured JSON logs, runtime metrics, request + query timing (ADR-0031) | OpenTelemetry |
| Schema | Alembic migrations; the audit-immutability triggers are a migration (ADR-0030) | Zero-downtime rollouts |
| API contract | Response models on every route; OpenAPI exported and checked; frontend types generated and asserted at compile time (ADR-0032) | Versioned API |
| Replay | PLAYBACK + RE_REASON against frozen tools | Cross-version replay |
| Evaluation | 167 scenarios + 88-mutation validation, gated in CI; §42 promotion gate | Larger benchmark |
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

  adversarial_security  34/34    recovery              14/14
  detection             10/10    refund_policy         28/28
  duplicate_payment     16/16    revenue_investigation 19/19
  failure_unknown       20/20    risk_approval          7/7
  payment_failure       14/14    webhook                5/5

median task latency 52 ms · mean grounding rate 1.0
```

**A suite that passes everything proves nothing on its own.** `make mutants`
deliberately breaks each core control and re-runs the suite:

```
87/88 mutations caught      complete run, 2026-09-08, 2h06m
  └─ 40 graded red by a named scenario · 2 detected as a crash
     45 by unit tests alone · 1 survivor

88/88                       after the survivor's test, verified individually
```

*Each mutant re-runs the whole scenario suite **and** the whole test suite, so a
complete run takes over two hours. This one ran against `3ebe32a` and is the first
complete run since ADR-0029 — the previous figure, 77/78, was measured on a tree ten
mutants and several modules ago and had been carried forward with a label saying so.*

*The survivor is worth more than the score. **"reconciliation: escalate actions that
already settled"** deletes the settled check inside `should_escalate`, and nothing in
626 tests noticed. Three of that function's four callers make the check redundant —
`escalate_exhausted` filters settled rows out in SQL, and both sweep call sites are
already inside an `if state in UNSETTLED` branch. The fourth does not: `reverify`
calls it unconditionally, after deciding what the read found. So an operator who
presses Re-verify on an UNKNOWN action four times and gets a real SUCCESS on the fifth
crosses the attempt limit on the attempt that resolved it — and the same action is
marked COMPLETED with "Re-verification resolved the action: SUCCESS" while being handed
to a human as "still unestablished". A finished refund on the escalation queue is how a
queue stops being read.*

*`test_a_manual_reverify_that_finally_succeeds_does_not_escalate` closes it, verified
the only way this can be: mutant applied by hand → red, reverted → green. **88/88 is
therefore two measurements, and it is stated as two** — 87 from the complete run, one
from a hand-verified mutant added afterwards. Adding a test cannot un-catch a mutant,
so the 87 still hold, but a full re-run against this exact tree has not been done and
the number is not presented as though it had.*

**§22's five browser journeys run.** `make e2e` stands the whole stack up
against its own database — seed, API, the built bundle behind `vite preview`,
one Chromium — and drives detection→incident, the approval gate through to
independent verification, a rejection, an UNKNOWN queue, and a replay. Two of
them assert a **negative** — that no external call was made — which is the
property a UI bug can violate while looking entirely correct.

These run in CI as the `browser` job, through the same
[`scripts/run_e2e.sh`](scripts/run_e2e.sh) that `make e2e` uses — one fixture,
two machines, rather than a workflow that reimplements half the script and
drifts from it. Not in `make ci`, deliberately: that is the check somebody runs
before pushing, and a browser download plus a second Postgres database would
make it slow enough to be skipped. §24 puts browser E2E before deploy, not
before every commit. See [ADR-0034](docs/adr/0034-browser-e2e-runs-in-ci.md),
which records this reversing the CI half of ADR-0015.

**Accessibility is measured, not asserted.** The same suite scans five screens
and the error state with axe in both themes, and found three real defects on its
first run — a contrast ratio measured against a surface the text no longer sat
on, a link distinguishable only by colour, and a loading skeleton that was
silent to screen readers because ARIA prohibits `aria-label` on a bare `<div>`.
None was reachable from jsdom.

**§20's twenty mandatory adversarial scenarios are audited rather than assumed**
— [`docs/adversarial-coverage.md`](docs/adversarial-coverage.md) counts what the
suite covers. The one real gap it found — an out-of-order webhook, which §14
requires handling and nothing exercised — is now closed by three tests, verified
against a hand-applied "believe the payload" defect that turns them red. Two
more (stale action, customer attempt limit) are covered by unit tests rather
than as scenarios, and that distinction is recorded rather than smoothed over.

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

Test suite: **491 passed** (`make test`) across unit, security and integration, in
under 15 seconds — the suite seeds once and rolls each test back, rather than rebuilding
the schema for every test.

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
                    │ Typed Tool Layer │  15 tools, strict schemas
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
- **A webhook decides when to look, never what was found.** This used to read
  "there are no webhooks, deliberately" — written before ADR-0018 added them,
  and left standing while two other sections of this file documented signed
  ingestion and a durable event store. The original reasoning survives the
  change: a webhook is something you are *told*, and being told is weaker than
  reading state back. So a relevant event marks an action for immediate
  re-verification and verification goes and reads the provider. An attacker who
  defeats the signature can make us re-read state we would have read anyway.

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
| One live refund per payment | partial `UNIQUE` index, not a prior `SELECT` | `test_two_approvals_for_one_payment_produce_one_refund` |
| Signing key on deployments | `require_configured_secret`, at import | `TestDevelopmentSecretIsRefusedOnDeployments` |
| Argument validation | before policy touches the database | `test_malformed_arguments_rejected_*` |
| Secret redaction | `app/audit/trace.py` | `test_secrets_are_redacted_from_traces` |
| Loop budget | 12 tool calls / 8 turns / 60s | `test_budget_terminates_runaway_loop` |

Two of those are recent and were found by review rather than by the suite, which
is worth saying plainly because the suite is what this project asks to be judged
on. Neither was reachable by a single-threaded test: the double-refund race
needs two connections committing against each other, and the signing-key control
was a docstring describing a function that did not exist. `tests/integration/
test_concurrency.py` is the first test here to open two real connections, and it
reproduces the double refund when the constraint is removed — two threads, both
reporting success, two refunds against one payment.

The injection claim is deliberately narrow: **"no external call occurred and the
decision was recorded"** — not "the agent resisted". Threat model:
[`docs/threat-model.md`](docs/threat-model.md).

---

## ⚙️ Setup

Requires Python 3.12+ and PostgreSQL.

```bash
createdb merchantops                     # or use the DATABASE_URL of your choice
cp .env.example .env                     # optional; defaults work locally
make setup                               # venv + the locked dependencies
make migrate                             # schema + the controls over it (ADR-0030)
make openapi                             # export the API contract consumers read
make seed                                # deterministic dataset
make test                                # 626 tests
make eval                                # 167 scenarios, on merchantops_eval
make mutants                             # prove the suite catches regressions
make harden                              # verify audit immutability on a live database
make ci                                  # the fast pre-push subset (see below)
make demo                                # full end-to-end walkthrough
```

Every one of those is safe to run with the console open. **None of them was
until 2026-09-08.** The evaluation suite drops and rebuilds the schema once per
scenario and inherited `DATABASE_URL`, so `make eval` destroyed the development
database 167 times; `make mutants` runs the suite per mutant and did it 88
times over; and `make ci` began by force-seeding the same database. Anyone
browsing at the time watched their open task become "Unknown task" with nothing
connecting the two events.

Each check now has its own database, created on demand: `<database>_eval` for
the suite (`EVAL_DATABASE_URL` to override) and `<database>_ci` for `make ci`
(`CI_DB`). `scripts/run_e2e.sh` had had one from the start and said why in a
comment — the reasoning existed and had simply never been applied one directory
across. `run_all()` additionally refuses to reset a database whose name does not
look disposable, so bypassing the entry point cannot reintroduce it; the default
makes the right thing happen and the guard makes the wrong thing impossible.

`make ci` is **not** everything CI runs, and used to say it was. It is seed,
harden, lint, clean-room import, tests, published-number check and evaluation —
the part that is fast enough to run before pushing. CI additionally runs the
migration driver against an unstamped database, the OpenAPI and generated-type
contract checks, the frontend's lint, typecheck, tests and audit, the browser
journeys and accessibility scans, the dependency lock and audit gates, and the
88-mutant run. Those need Postgres service containers, a browser download and
over two hours, which is why they are gated there and not here — and why the
name was worth correcting rather than leaving as a claim nobody re-read.

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
make web-test                     # 295 Vitest tests
make web-lint                     # eslint — rules-of-hooks, exhaustive-deps
make web-audit                    # npm audit, high and above
```

`make web-lint` is the frontend's ruff, and curated the same way. `tsc --noEmit`
proves the types line up and says nothing about a `useEffect` that reads a value it
never declared — which, on three screens that poll through `useLiveRefresh`, is a
queue refreshing on the wrong schedule behind a "live" indicator.

What it enforces is deliberately small: rules-of-hooks, `exhaustive-deps` (promoted
from the plugin's own *warn* to an error), unused variables, and the recommended
TypeScript set. `eslint-plugin-react-hooks` v7's recommended set is largely the React
Compiler rule set; turned on wholesale it reports ten findings here — six guarded
`setState` calls in effects that clear derived state when its input disappears, two
`Date.now()` reads during render, and the latest-callback ref in `useLiveRefresh`.
All ten were read. None is a defect: they are deviations from rules that exist so the
React Compiler can memoize aggressively, and this app does not use the compiler.
They are off by name, with the reasoning in
[`web/eslint.config.js`](web/eslint.config.js), so that adopting the compiler later
starts from a written list rather than a rediscovery.

The SPA is outside the contract's MVP scope (§3, §52) and exists by explicit request —
see [ADR-0015](docs/adr/0015-react-spa-frontend.md). The Streamlit UI remains the
contract-conformant surface. ADR-0015's consequence that "nothing in `web/` affects the
Python CI jobs" is now historical: the `contract` job type-checks the frontend against
the generated OpenAPI types and runs the Vitest suite, the `browser` job runs the five
journeys and the accessibility scans ([ADR-0034](docs/adr/0034-browser-e2e-runs-in-ci.md)),
and a stale `src/api/schema.d.ts` fails the build. These are regression gates, not local
ones.

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
| `GET /trace/{correlation_id}` | §58 — everything one **operation** touched, in one ordering |
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
| `GET /actions` | **The Action Center** — the queue in five sections, one read |
| `GET /command-center` | **What needs attention** — revenue health, funnel, live activity |
| `GET /search?q=` | One box, every identifier. Exact match, merchant-scoped in SQL |
| `GET /payments/{id}/lifecycle` | **§7 — one payment, end to end**, across every correlation id it spans |
| `GET /scenarios` · `POST /scenarios/{id}/run` | Evaluation suite |
| `GET /health` | Reports active LLM provider and payment adapter |
| `GET /liveness` | The process is running. No I/O, no dependencies |
| `GET /readiness` | Per-component dependency verdicts; 503 when a *required* one is down |

Every endpoint enforces authentication and merchant isolation server-side. A
cross-merchant read returns 404, not 403 — existence is not leaked.

`GET /liveness` and `GET /readiness` are unauthenticated because a platform probe
cannot hold a token. `/readiness` publishes *verdicts* to anyone and the operational
detail behind them — mapping coverage, the reconciliation backlog, which payments
drifted — only to a caller with a valid token, which is the same line
`/metrics/prometheus` already draws. An invalid token narrows the body rather than
failing the request: a probe with a stale credential must not take a deployment out of
rotation.

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
5. **Single-process, synchronous.** No queue, no horizontal scale. The plan's §8
   topology (API → queue → worker) is designed and not built; what stands in for the
   worker is the cron sweep, and `/readiness` now reports when that sweep has stopped
   running rather than leaving a silent backlog to be discovered by an operator.
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
12. **Two detection rules cannot fire on the seeded dataset**, for different reasons and
    both stated rather than hidden.

    *Provider burst* reads `webhook_events`, and the seed is payment history — it carries
    no provider events. Exercised by constructed state in tests and in `CLS-01`/`CLS-02`.

    *Unusual refund activity* is the opposite case: the seed **does** carry refunds, and
    they are flat — nine in each window at similar value. The rule correctly says nothing,
    which is a rule discriminating rather than one that cannot run. A test asserts the
    silence, so a future change that makes it fire on ordinary business fails.
13. **`confidence` is a display value.** It is recorded and shown and consulted by nothing.
    Against the deterministic planner it is computed from evidence count, which measures
    the planner rather than any judgement; against a real model it would mean something
    different and should be reported separately.
14. **Authentication is HMAC bearer tokens, not an identity provider.** Tokens are
   unforgeable and permissions are read from the database on every request, but
   there is no expiry, rotation, revocation list, or audience binding — revoking
   one person means rotating the secret for everybody. A deployment can no
   longer run on the development signing key, though: the application refuses to
   start where a platform marker says it is not a laptop.
15. **Two of §12's eight signal types are still not implemented.** Absolute thresholds
   and explicit time clustering have no rule of their own — clustering exists inside the
   duplicate rule as a window, and every other rule compares against a *baseline* rather
   than a fixed number. Implemented: baseline deviation, payment-method degradation,
   percentage change, duplicate detection, failure-code spikes, unusual refund activity.
16. **Detection observes state, not a stream.** `webhook_events` stores what the provider
   *tells* us, but the detection rules still read `payments`. A business change that
   never lands on a payment row is invisible to them. Wiring detection onto the event
   store is real work, not a rename.
17. **Detection is a sweep, not a daemon** — same trade-off as reconciliation, above.
    Incidents appear at sweep cadence.
18. **Only 21 of 590 payments are externally mapped.** Refunds outside that set are
   correctly rejected as `not_externally_mapped` — that is the mapping layer working,
   not a defect. `/readiness` publishes the coverage rather than leaving it to be
   inferred from a rejection count, because "seventeen refunds were rejected" is
   ambiguous between a broken mapping layer and a working one applied to unmapped data.
19. **The mapping is recorded twice, and the disagreement is checked rather than
   assumed away.** `provider_mappings` is the control plane's authority; the
   `payments.external_*` columns are the *mock provider's* own store, which is the
   right place for them — the mock stands in for Razorpay, and Razorpay holds
   provider-side state. With live credentials the columns stop being read by anything
   but the mock. Until then two rows carry one fact, so
   `app.integrations.mapping.check_consistency` compares them and `/readiness` reports
   any drift as `degraded`.
20. **No mapping has ever been confirmed against a real provider.**
   `provider_mappings.verified_at` is null on every seeded row, and null means nobody
   has checked — deliberately a different claim from "checked and it was there". Real
   Test Mode credentials would populate it.

### Coverage limits

21. **Forty-six of the 88 mutants are caught by unit tests alone** — no scenario
    distinguishes them. Measured, not estimated: the run's own table reports the
    scenarios each mutant turned red, and 46 rows report none. They are the tooling
    controls, the read-side aggregates (metrics, ledger, taxonomy), the mapping and
    reconciliation guards, and the branches a safety check reaches first. Several are
    structural rather than an oversight — the deterministic planner does not compose
    customer contact on its own, so no scenario can drive execution-time opt-out or
    contact deduplication, and giving the planner that freedom would be a worse system
    in exchange for a better number. Two more (registry lookup, argument validation)
    are detected as a *crash* rather than a graded failure: the suite dies on
    `spec is None` instead of reporting SEC-24 red. That is detection — nothing
    silently passes — but it proves less than a graded failure does.
    Counted honestly: **40 of 88 produce a graded scenario failure**, 2 are detected as
    a crash, and 46 by unit tests alone. See
    [`docs/evaluation.md`](docs/evaluation.md) for the per-mutant breakdown.
22. **The 88-mutant run takes over two hours.** Each mutant re-runs the full scenario
    and test suites, and both have grown. The test half is fast (one seed, per-test
    rollback); the scenario half still rebuilds the schema per scenario, which is where
    the time goes. `scripts/mutation_test.py <substring>` runs a subset during
    development; CI runs all of them. The job's timeout was 50 minutes, which would
    have killed it partway and reported a timeout where the answer was "87 caught, 1
    survived"; it is now 180.

### Supply-chain limits

23. **`npm audit` reports zero, and that is a recent state rather than a standing
    property.** On 2026-09-07 it reported eight. Two were in a dependency that ships
    to the browser: `react-router` 6 carried `GHSA-337j-9hxr-rhxg` (SSR hydration,
    unreachable here — pure SPA, `createRoot`, no server renderer) and
    `GHSA-wrjc-x8rr-h8h6` (open redirect: a path beginning with a backslash treated
    as same-origin by `<Link>` and `useNavigate`, then followed off-site). The second
    was reachable in principle. Both are gone at `react-router` 7.18.3.

    `internalRoute()` in `CommandPalette.tsx` stays. It was written to close the open
    redirect locally when no upstream fix existed, and it is worth keeping now that
    one does: the palette is the only place a *whole* route arrives as data rather
    than as an id interpolated into a fixed template, and a guard on that input
    should not depend on which version of a router is installed. Two tests hold it.
24. **Six advisories in the build and test tooling were closed by upgrading it,
    not by widening the gate.** Before 2026-09-08 `npm audit` reported eight: the
    two above plus `vite` 5.4.21 (high, path traversal in optimized-deps `.map`
    handling), `esbuild` 0.21.5 (moderate, any website can request the dev server
    and read the response), `vitest` 2.1.9 (critical, arbitrary file read and
    execute while the Vitest UI server is listening) and three transitive on those.
    All six were dev-server or test-runner surface — none in `dependencies`, none in
    `dist/` — so none was urgent, and the temptation was to write that down and move
    on. Vite 8 / Vitest 5 removes all six, and drops `esbuild` from the tree
    entirely. It required raising CI's Node from 20 to 24 (Vitest 5 needs ≥ 22.12),
    which `engines` plus `engine-strict` now enforce at install rather than thirty
    seconds into a test run. Verified by the full suite: 295 Vitest tests, the build,
    and all eleven browser journeys and accessibility scans against `vite preview`,
    which is the part a Vite major could have broken silently.

    The router went the same way for the same reason, and stopped one major short of
    the newest on purpose: `react-router` 8 requires React ≥ 19.2.7, and folding a
    React major into an advisory fix would be smuggling a much larger decision
    through a security patch. 7.18.3 clears both advisories on React 18. `react-router-dom`
    is gone — v7 merged it back into `react-router`, and the imports now say so.
    It costs 28 kB raw / 9 kB gzipped in the bundle, which is recorded rather than
    discovered later.
25. **The Python dependencies were unpinned until 2026-09-08, so CI and
    development ran different code.** `requirements.txt` is thirteen `>=`
    constraints, and CI installed straight from it — resolving to whatever PyPI
    had that morning. Two runs of the *same commit* could therefore run different
    versions, which is a strange property for a repository whose evaluation suite
    publishes a number and whose CI asserts that number is reproducible. It was
    not hypothetical: when the lock was first generated, **nine packages differed**
    between the development venv and what CI would have installed, `anthropic` by
    four minor versions.

    `requirements.lock` is now the installed set — fully pinned, hashed,
    `--require-hashes` in every CI job and in `make setup`. `requirements.txt`
    remains the human declaration. `make lock` regenerates it and keeps existing
    pins; `make lock-upgrade` takes newer versions deliberately. Because uv
    prefers the pins already in the file, recompiling is a no-op unless
    `requirements.txt` changed — which is what makes "the lock is current" a real
    CI check rather than a race against PyPI. The locked set was verified before
    it was adopted: 626 tests and 167/167 scenarios against a scratch venv built
    from it.

    `pip-audit` is now blocking, which it could not honestly have been before —
    failing on a resolution that moves every morning really would have failed
    unrelated pull requests. A second, advisory audit covers the whole installed
    environment rather than the declared set; that one is what surfaced seven
    advisories in `pip` itself, which no requirements-file audit can ever see.
26. **Nothing audited the npm dependencies until 2026-09-07.** `pip-audit` has run in
    CI since the start; the web half had no equivalent, which means every advisory
    above had been open and unread rather than open and accepted. `make web-audit`
    now fails on a high or critical advisory in production dependencies, and `make
    web-audit-all` prints everything including dev. The gate deliberately stops at
    `--omit=dev --audit-level=high`: what ships to a browser is held to a hard line,
    the toolchain is reported and reasoned about rather than blocking a merge, and a
    gate that fires on findings nobody has agreed to is a gate people learn to pass
    with `--force`.

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
  observability/ structured logs, runtime metrics, request + query timing
  api/          FastAPI surface + response contracts (ADR-0032)
alembic/        schema migrations + the audit-immutability control
ui/             Streamlit app
web/            React SPA — Vite + TypeScript (ADR-0015), 295 tests
data/           167 scenarios + the last evaluation report
scripts/        migrate, seed, spike, scenarios, demo
tests/          unit · security · integration  (626 tests)
docs/           MerchantOps.md (governing spec), CONTRACT.md (superseded),
                architecture (+ assumptions), threat model, evaluation,
                gap-closure plan, 33 ADRs
```

## 📄 License / disclaimer

Licensed under the MIT License — see [`LICENSE`](LICENSE).

Independent developer project, provided as-is for demonstration purposes. Uses
Razorpay Test Mode APIs where applicable. Not affiliated with, sponsored by, or
endorsed by Razorpay. No real-money transactions are performed anywhere in this
codebase.

Operating it: [`docs/runbook.md`](docs/runbook.md) — health checks, the
reconciliation and escalation queues, migration hazards, the backup/restore
drill (rehearsed, and honest about what it does not tell you), and triage for
the two objectives whose target is zero.

Security reports: [`SECURITY.md`](SECURITY.md).
