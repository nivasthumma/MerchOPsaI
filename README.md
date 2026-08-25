# 🏦 MerchantOps Agent

[![CI](https://github.com/nivasthumma/Merchantagent/actions/workflows/ci.yml/badge.svg)](https://github.com/nivasthumma/Merchantagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-81%20passed-brightgreen.svg)](#-measured-results)
[![Scenarios](https://img.shields.io/badge/scenarios-106%2F106-brightgreen.svg)](#-measured-results)
[![Mutations caught](https://img.shields.io/badge/mutations%20caught-15%2F15-brightgreen.svg)](#-measured-results)

An AI agent that investigates merchant payment and revenue problems, recommends a
corrective action, and — only with human approval — executes it through a controlled
tool, then independently verifies what actually happened.

> **Independent developer project. Uses Razorpay Test Mode APIs where applicable.
> Not affiliated with, sponsored by, or endorsed by Razorpay.**

The point of this project is not the chatbot. It is the **trustworthy action loop
around the agent**:

```
OBSERVE → REASON → DECIDE → POLICY CHECK → HUMAN APPROVAL → ACT
        → VERIFY → AUDIT → REPLAY → EVALUATE
```

---

## 📑 Contents

**Start here** — what is real, what is measured, and what it looks like running:

| | |
|---|---|
| [🧭 Built vs designed](#-built-vs-designed) | What ships today vs what is architecture |
| [⚠️ Two honesty disclosures](#-two-honesty-disclosures) | Mocked execution, and what the metrics measure |
| [📊 Measured results](#-measured-results) | 81 tests · 106/106 scenarios · 15/15 mutations |
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
| Agent | One bounded agent, 6 typed tools | Specialised multi-agent orchestration |
| Reasoning | Provider abstraction: Anthropic (`claude-opus-5`, adaptive thinking, prompt caching) **or** a deterministic planner. Credential detection covers all four SDK sources | Model routing, cost-aware selection |
| Policy | Deterministic engine: RBAC, merchant isolation, risk, amount limits, duplicate guard | Per-merchant configurable policy, approval chains |
| Approval | Server-side, expiring, re-checked at execution | Multi-party approval |
| Execution | Razorpay Test Mode adapter **or** deterministic mock (see below) | Production integration |
| Verification | Independent read-back with SUCCESS/FAILED/PARTIAL/UNKNOWN | — |
| UNKNOWN | First-class, **resolvable**; reconciliation sweep + escalation queue | Always-on worker (needs a queue) |
| Audit | Append-only **enforced by PostgreSQL**, secrets redacted | Distributed tracing |
| Replay | PLAYBACK + RE_REASON against frozen tools | Cross-version replay |
| Evaluation | 106 scenarios + 15-mutation validation, gated in CI | Larger benchmark |
| Data | Seeded synthetic dataset, 2 merchants | Streaming / generated datasets |
| UI | Streamlit **and** a React SPA (`web/`) | Next.js, SSR |
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
106/106 scenarios passed      (critical: 59/59)

  adversarial_security  25/25    payment_failure       12/12
  duplicate_payment     14/14    refund_policy         25/25
  failure_unknown       18/18    revenue_investigation 12/12

310 assertions · median task latency 39 ms · mean grounding rate 1.0
```

**A suite that passes everything proves nothing on its own.** `make mutants`
deliberately breaks each core control and re-runs the suite:

```
15/15 mutations caught
```

That run is what makes the 106/106 meaningful — and it is how three real gaps
were found and closed (see below).

Configuration: `llm_provider=deterministic`, `payment_adapter=mock`,
`dataset=synthetic-v1 (seed 20260825)`. Counts are reported rather than percentages.
Verified reproducible: two consecutive runs produce an identical pass/fail vector.

Test suite: **81 passed** (`make test`) across unit, security and integration.

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

4. **Reconciliation is a sweep, not a daemon.** An always-on worker means Redis or
   Celery, which the MVP scope excludes. Actions settle at sweep cadence, not
   instantly — bounded, escalated, and visible, but not real time.
5. **Single-process, synchronous.** No queue, no horizontal scale.
6. **Rate limiting is per-worker.** The counter is in-process, so with several
   workers the limit is approximate. A shared counter needs Redis.
7. **Authentication is HMAC bearer tokens, not an identity provider.** Tokens are
   unforgeable and permissions are read from the database on every request, but
   there is no expiry, rotation, revocation list, or audience binding.
8. **Only 21 of 589 payments are externally mapped.** Refunds outside that set are
   correctly rejected as `not_externally_mapped` — that is the mapping layer working,
   not a defect.

### Coverage limits

9. **Three of the 15 mutants are caught by unit tests only** — no scenario
   distinguishes them: idempotency-key derivation, the duplicate-action SAVEPOINT,
   and the key-name branch of audit redaction. Each is reachable in principle but
   sits behind a guard that fires first in every path a scenario can drive. Two more
   (registry lookup, argument validation) are detected as a *crash* rather than a
   graded failure — the suite dies on `spec is None` instead of reporting SEC-24 red.
   Counted honestly: 10 of 15 produce a graded scenario failure. See
   [`docs/evaluation.md`](docs/evaluation.md) for the per-mutant breakdown.

---

## 🗺️ Roadmap

Ordered by value, not by architectural impressiveness:

1. Complete the Razorpay Test Mode spike with real credentials; map genuine captured
   payments; publish a second results table for real execution.
2. Run the suite against `claude-opus-5` and publish model-vs-harness results side
   by side, including replay consistency.
3. ~~Background reconciliation for `UNKNOWN` actions.~~ **Done** — sweep +
   escalation queue, see below.
4. ~~Expand to 100 scenarios and wire into CI.~~ **Done** — 106 scenarios,
   mutation testing, GitHub Actions gate.
5. Per-merchant configurable policy.
6. Replace the header-based principal with a real identity provider.

---

## 📁 Repository layout

```
app/
  agent/        runtime (bounded loop), approval, replay, versioned prompts
  tools/        typed registry, contracts, investigation + action tools
  policy/       deterministic policy engine
  verification/ read-back verification and state classification
  integrations/ razorpay adapter + fault-injection seam
  llm/          provider abstraction (anthropic | deterministic)
  eval/         scenario schema + runner
  api/          FastAPI surface
ui/             Streamlit app
web/            React SPA — Vite + TypeScript (ADR-0015)
data/           106 scenarios + the last evaluation report
scripts/        seed, spike, scenarios, demo
tests/          unit · security · integration  (81 tests)
docs/           CONTRACT.md, architecture (+ assumptions), threat model, evaluation, 14 ADRs
```

## 📄 License / disclaimer

Licensed under the MIT License — see [`LICENSE`](LICENSE).

Independent developer project, provided as-is for demonstration purposes. Uses
Razorpay Test Mode APIs where applicable. Not affiliated with, sponsored by, or
endorsed by Razorpay. No real-money transactions are performed anywhere in this
codebase.

Security reports: [`SECURITY.md`](SECURITY.md).
