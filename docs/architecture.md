# Architecture

## Context and problem

A merchant operations team needs to answer questions like "why did revenue drop this
week?" and "did we double-charge this customer?" — and then *act* on the answer. The
acting part is where AI systems usually become unsafe: a language model that can call
a refund API is one hallucination away from moving real money.

The design premise here is that **the model is a reasoning component inside a
deterministic system**, not the system's authority.

## The core loop

There are two entry points, and they converge immediately.

```
  Payments (the observed history)        Merchant request
              │                                 │
              ▼                                 │
    deterministic detection                     │
              │                                 │
              ▼                                 │
          anomaly                               │
              │                                 │
              ▼                                 │
   incident  (lifecycle, §13)                   │
              │                                 │
              └────────────┬────────────────────┘
                           ▼
                intent + bounded plan
                           ▼
             read-only investigation tools
                           ▼
             evidence synthesis (typed Findings)
                           ▼
                    recommendation
                           ▼
             risk classification         ← declared floor, raised by computed factors
                           ▼
             policy decision             ← deterministic, outside the model
                           ▼
             human approval              ← server-side, expiring; two people if CRITICAL
                           ▼
             action via typed tool       ← idempotency reserved BEFORE the call
                           ▼
             independent verification    ← reads back business state
                           ▼
                     audit trace
                           ▼
                        replay
```

An incident supplies **context**, never authority. Both entry points run the same runtime,
the same policy engine and the same audit trail; nothing reachable from an incident is
reachable only from an incident.

## Recovery planning

Added in ADR-0020. §23's flow, and it ends where §23 ends:

```
incident -> affected transactions -> eligibility -> expected recovery -> risk -> candidates
```

Planning does not execute. A candidate is acted on through the ordinary
single-action path, so there is no second way to move money — `dispatch_candidate`
adds only what an ordinary task lacks: §27's bounds and §28's rules, checked
immediately before dispatch and acted on rather than logged.

**The numbers obey §49's ordering**, and that ordering is a claim about the world:

```
revenue at risk  >=  eligible recovery  >=  expected recovery  >=  actual recovery
```

Volume is attributed before it is counted. Detection measures at-risk as the value of the
*excess* failures; some payments fail on the best of days and were never at risk from this
incident. So `eligible = eligible_volume x (revenue_at_risk / total_failed_volume)`, and
`expected = eligible x baseline_success_rate`. `actual` is populated only from a verified
SUCCESS — an UNKNOWN action has not been shown to have moved anything.

**Stopping distinguishes two outcomes**, because collapsing them loses the one that matters:

| | means | examples |
|---|---|---|
| `STOP` | finished, or not worth continuing | budget exhausted, action count reached, expected recovery below threshold |
| `ESCALATE` | the campaign cannot safely decide alone | provider unavailable, risk above the ceiling, evidence insufficient |

Bulk campaigns escalate rather than run: more than one financial action in one campaign is
CRITICAL (§24), which is above the automation ceiling. Automated recovery does not perform
bulk refunds; a human does.

## Risk and approval

Added in ADR-0019. MerchantOps §24 makes risk a function of the call, not a constant per
tool — but only in one direction:

```
final_risk = max(declared class, computed risk)
```

Computed risk may **raise** a call above its tool's declared class. It may never lower one.
The declared class is fixed in the registry; the computed part reads arguments, and
arguments come from the model. A computed score that could lower risk would give
model-supplied input a path to weaken a control — an injected instruction that made an
action merely *look* small would buy a softer gate.

| Factor | Raises to | Notes |
|---|---|---|
| irreversibility | MEDIUM | declared on the tool; a refund cannot be undone |
| financial value | HIGH | fraction of the merchant's own limit; §24 grades ₹5,000 as HIGH |
| uncertainty | CRITICAL | a further action on a payment whose last action never settled |
| bulk size | CRITICAL | more than one action in one campaign; supplied by the recovery planner |

Value alone never reaches CRITICAL. §24's own example reserves CRITICAL for *bulk* — it is
about breadth, not the size of one transaction — and a single refund at the top of its
permitted range is the most serious ordinary action, not an extraordinary one. Breadth is
its own dimension because a single mistake in a campaign repeats itself.

`CRITICAL` maps to `REQUIRE_DUAL_APPROVAL`, and dual approval is a **UNIQUE constraint**:

```sql
UNIQUE (approval_id, user_id)   -- approval_signatures
```

Two approvers enforced in application logic is a check a retry or a race can get past.
Enforced by the database, one person signing twice is not a case to remember to reject —
it is a write that cannot succeed. One signature records and returns with nothing external
touched; the second signer still passes every gate; and one veto is enough, because
requiring consensus to *stop* would make the extra approver weaker than a single one.

## Webhooks — evidence, not authority

Added in ADR-0018. MerchantOps §34's pipeline, with one rule holding it together:

```
delivery -> signature -> dedup -> durable store -> processing -> reconciliation
                                                        │
                                    the webhook says WHEN to look
                                    the adapter says WHAT was found
```

Nothing in `app/webhooks/` writes `verification_state` from a payload. A verified,
subscribed event finds the actions touching that entity and re-reads provider state
through the adapter. The payload's own `status` is stored and never consulted.

That is why forging a delivery is uninteresting: an attacker who defeats the signature can
make the system re-read state it would have read anyway, and cannot say what that state is.

| Outcome | When | Acted on |
|---|---|---|
| `PROCESSED` | signed, subscribed type, entity we own | yes — re-verification runs |
| `IGNORED` | no subscriber, unknown entity, **or no secret configured** | no |
| `INVALID` | signature failed | no — stored for investigation |
| `DUPLICATE` | `event_id` already seen | no |

Every delivery is stored, including the refused ones: a rejected webhook that leaves no row
is an attack nobody can investigate. The merchant is resolved from our own records, never
from the envelope's `account_id` — reading it from the body would let a forged delivery
address another tenant.

When re-verification regresses an action away from `SUCCESS`, that is §35's reconciliation
incident: `RECONCILIATION_MISMATCH`, raised at `CRITICAL`, **with no correction applied**.
Overwriting the record would erase the only evidence the two ever disagreed. `UNKNOWN` is
excluded — that is a failure to read, not a disagreement, and it belongs to the sweep.

## Detection and incidents

Added in ADR-0017. MerchantOps §12 is explicit that the model does not inspect raw events:

```
    many events -> deterministic detection -> anomalies -> incidents -> LLM
```

`app/detection/rules.py` holds the rules. Each is a SQL query and arithmetic — no model
involvement, and none possible. Two rules run today:

| rule | fires when | at-risk figure |
|---|---|---|
| `success_rate_below_baseline` | a method's success rate falls ≥10pp below its own prior-period baseline, over ≥30 attempts | `(expected − actual successes) × avg transaction value` (§22) |
| `duplicate_capture_on_order` | two captured payments, same order/customer/amount, inside a window | unrefunded exposure |

Three properties are load-bearing:

- **Idempotent.** `Incident.detection_key` is UNIQUE and derived from the anomaly's facts,
  not the clock. Re-running the sweep collides rather than creating a second incident —
  the same mechanism `agent_actions.idempotency_key` uses for execution, applied to
  observation.
- **Discriminating.** A volume floor and a threshold keep ordinary variance out. Onset
  detection works on much smaller hour buckets and therefore uses a wider margin; without
  that it reports the first noisy hour as the incident's start.
- **Deterministic status.** The lifecycle (`app/incidents/lifecycle.py`) writes its legal
  transitions out rather than deriving them from an ordering, so that an incident in
  `UNKNOWN` can still reach `RESOLVED` when reconciliation settles its actions. `CLOSED`
  is the only terminal state.

### What moves an incident

```
task status  ->  incident status      deterministic, app/incidents/manager.py
model prose  ->  incident status      never
```

An agent that concludes "this is resolved" resolves nothing. Investigation stops at
`ROOT_CAUSE_IDENTIFIED`; everything past it belongs to the recovery planner (§23), which
is not built.

## Request path in detail

### Caller to execution

```
   Merchant
      │
      ▼
   MerchantOps API
      │   Authorization: Bearer <token>
      │      ├─ verify HMAC signature      ──► 401 invalid token
      │      ├─ resolve subject in the DB  ──► 401 unknown principal
      │      └─ rate limit (per principal) ──► 429 too many requests
      │
      │   Identity is FIXED here, before the agent exists.
      ▼
   ┌──────────────────────────────────────────┐
   │            Agent Runtime                 │
   │                                          │
   │  LLM provider   anthropic | deterministic│
   │  System prompt  investigator-v1 (pinned) │
   │  Tool defs      6 typed, strict schemas  │
   │  Task context   request · tool results   │
   │                 policy results · approval│
   │                                          │
   │  NOT in context: secrets, credentials,   │
   │  unscoped merchant data, provider ids    │
   │                                          │
   │  Budget: 12 tool calls · 8 turns · 60s   │
   └──────────────────┬───────────────────────┘
                      │
                 tool request        ← a REQUEST, never a decision
                      │
                      ▼
 ══════════════════ TOOL GATEWAY ══════════════════════════════════════
                      │
   1. Registry lookup │
      is this a registered tool?
                      ├── no ──────────────► TOOL_UNAVAILABLE
                      │                      no dynamic dispatch exists
                      ▼
   2. Argument validation
      types · ranges · enums · unknown keys
                      ├── invalid ─────────► TOOL_INVALID_ARGUMENT
                      │                      MUST precede gate 3
                      ▼
   3. Policy engine   (deterministic; reads no model output)
      ├─ permission ──────────────────────► DENY missing_permission
      ├─ merchant ownership ──────────────► DENY merchant_isolation
      ├─ risk classification  (from the registry, not the model)
      ├─ amount / balance limits ─────────► DENY amount_limit_exceeded
      │                                     DENY exceeds_refundable_balance
      │                                     DENY payment_not_refundable
      └─ duplicate-action guard ──────────► DENY duplicate_action
                      │
                      ▼
   4. Approval gate   HIGH risk only
                      ├── HIGH ───────────► HALT
                      │                     approval record created,
                      │                     loop stops, no external call.
                      │                     Resumes only via a human calling
                      │                     approve_and_execute()
                      ▼
   5. Execute         LOW risk only from the loop
                      │
                      ▼
   6. Persist         tool_call row + audit events, every outcome
 ═══════════════════════════════════════════════════════════════════════
                      │
                      ▼
              Tool execution
```

Every exit above is a *recorded* outcome, not a silent drop: each writes a
`tool_calls` row with its error code and an audit event. A denial the trace cannot
show is indistinguishable from a bug.

Two placements in that diagram are load-bearing.

**Authentication is upstream of the agent, not inside the gateway.** The principal is
established at the API boundary and handed to `AgentRuntime` as a constructor
argument. The agent *receives* an identity; it cannot supply, assert or influence
one. Putting authentication downstream of the model would imply the tool request
carries identity — which would make the model a participant in deciding who it is.

**The approval gate terminates the loop.** For a HIGH-risk action there is no path
from the agent to execution. Policy returns `REQUIRE_APPROVAL`, the loop halts, and
execution is reachable only through `approve_and_execute()`, which a human triggers
and which re-runs every check server-side. `execute_read_tool` has no implementation
for HIGH-risk tools either, so an erroneous `ALLOW` still could not perform one.

### Why the gate order matters

**Why argument validation precedes policy.** The policy engine queries the database
using the tool's arguments (payment ownership, refundable balance). Passing an
unvalidated model-supplied value into that query is both a crash and an injection
surface. This ordering was not in the original contract; it was found by scenario
SEC-04, which passed `synthetic_payment_id: 12345` (an integer) and produced a
`ProgrammingError` from PostgreSQL. The fix is recorded here because the ordering is
load-bearing, not incidental.

**Why the registry lookup is first.** It is what makes "the model cannot call an
unregistered tool" a fact rather than an aspiration: there is no dynamic dispatch,
no name-to-callable map built from model output, and nothing to fall through to. A
tool that is not in `REGISTRY` has no execution path at all.

**Why risk comes from the registry, not the request.** Each tool declares its own
risk class. Nothing infers risk from what the model says it intends, so a request
cannot describe itself into a lower tier.

**Why the approval gate halts rather than branches.** Returning `REQUIRE_APPROVAL`
ends the agent's turn. Execution is not a later branch of the same loop — it is a
separate entry point (`approve_and_execute()`) that re-runs authentication, approval
validity, expiry, full policy evaluation and the payment's preconditions before
touching the provider.

## Data architecture

```
Synthetic dataset  ──→  investigation, evaluation      the analytical truth
                        (revenue, failures, duplicates)

Mapping layer      ──→  synthetic_payment_id → external_payment_id

Razorpay Test Mode ──→  execution, state verification  the action surface
```

Razorpay Test Mode is **not** an analytics source. A test account has no organic
revenue trend, no UPI failure concentration, and no naturally occurring duplicate
payments. Claiming otherwise would misrepresent the whole investigation layer.

The mapping layer is the only bridge. `resolve_external_payment()` is the single
function that converts a synthetic id to a provider id, and it enforces merchant
ownership and mapping existence. The agent has no way to name a provider id.

### End-to-end data flow

```
        Synthetic dataset                      the analytical truth
   customers · orders · payments · failures
   duplicate scenarios
   (revenue is COMPUTED from payments —
    there is no revenue table)
                  │
                  ▼
       MerchantOps DB (PostgreSQL)
   business data  +  execution state
   agent_tasks · tool_calls · agent_actions
   approvals · audit_logs · evaluation_results
                  │
                  ▼
              AI Agent                         bounded loop, budget-capped
                  │
            Investigation                      read-only typed tools
                  │
            Recommendation                     typed Findings, each cited
                  │
              Policy Gate ──────────────────►  DENY      no external call
                  │
               Approval ─────────────────────►  REJECT    no external call
                  │
                  ▼
          ┌─────────────────┐
          │  Mapping layer  │  SYN_PAY_xxxx → pay_xxxx    ◄── REQUIRED (§6)
          └────────┬────────┘
                   ▼
           Razorpay Adapter                    Test Mode | mock
                   │
                   ▼
          Razorpay Test Mode
          payments · refunds · orders
                   │
                   ▼
             Verification                      reads the PAYMENT back,
                   │                           not the create response
     ┌─────────┬───┴─────┬──────────┐
     ▼         ▼         ▼          ▼
  SUCCESS   FAILED    PARTIAL    UNKNOWN
                         │          │
                         └────┬─────┘
                              ▼
                       Reconciliation          re-runs verification,
                       cron / on demand        reconciling by
                              │                idempotency key
                    ┌─────────┴─────────┐
                    ▼                   ▼
                 settled          escalated to the
                                  operator queue
                              │
                              ▼
                       Audit  ·  Replay
```

### Four things this diagram is careful about

**1. Four terminal states, not three.** `PARTIAL` is not decorative: the provider
can accept a refund while the payment's `amount_refunded` never moves. Collapsing it
into SUCCESS or FAILED puts a real state in the wrong bucket, which is exactly what
the four-state model exists to prevent.

**2. Verification precedes reconciliation.** Verification is the primitive and runs
immediately after every action. Reconciliation is a bounded retry loop *around* it,
entered only for `UNKNOWN` and `PARTIAL`. Drawing reconciliation first would imply an
action can never settle promptly, which is wrong.

**3. The mapping layer is on the critical path.** It is the only route from a
synthetic id to a provider id, and it enforces merchant ownership. Drawing the agent
straight through to the adapter would imply the agent can name provider ids — which
the design forbids.

**4. There are no webhooks, deliberately.** A webhook is *something you are told*.
The verification thesis is *read the state back yourself* — which is why
`verify_refund` reads `payment.amount_refunded` rather than trusting the refund-create
response. A webhook belongs to the same class as that response: spoofable, replayable,
reorderable, droppable. It would buy **latency, not truth**, and you would still have
to verify.

If webhooks are added later, the correct shape is a *trigger* sitting beside the cron
trigger on reconciliation — never a source feeding verification:

```
   cron ────────┐
                ├──► Reconciliation ──► Verification ──► state
   webhook ─────┘        (trigger only; never trusted as evidence)
```

The cost of that path is a publicly reachable endpoint, signature verification, replay
protection, out-of-order handling and idempotent processing — real work for a latency
win, in a system whose stated limitation is already "settles at sweep cadence".

## Agent runtime

One bounded agent. Not five.

- **Budget**: 12 tool calls, 8 LLM turns, 60s wall clock. Exceeding any of these
  terminates in `ABORTED_BUDGET` with the partial trace preserved. An unbounded agent
  loop is an unbounded spend and an availability failure.
- **Context**: the agent receives the request, tool results, policy results and
  approval status. It never receives secrets, credentials, or unscoped merchant data.
- **Untrusted data**: free-text merchant fields are tagged `untrusted` at the tool
  boundary and rendered inside `<untrusted_merchant_data>` delimiters. They are never
  interpolated bare into the prompt.

## Provider abstraction

`app/llm/` exposes one interface: `turn(system, messages, tools) -> LLMTurn`.

- `AnthropicProvider` — `claude-opus-5`, adaptive thinking, strict tool schemas.
- `DeterministicProvider` — a rule-based planner used when no API key is present.

A **manual agentic loop** is used rather than the SDK's tool runner. This is a
deliberate departure from the SDK's default recommendation, justified by three
requirements the runner does not expose: policy interception between the tool request
and execution, frozen-tool replay, and full per-step trace persistence with budget
enforcement. The tradeoff is documented in ADR-0009.

## Policy engine

Deterministic, database-backed, and completely independent of model output. It reads
only: the authenticated principal, the tool's declared risk, the tool's arguments
(post-validation), and database facts.

Risk is a property of the **tool**, declared in the registry — never inferred from
what the model says it wants to do.

```
LOW    read metrics, read order, find duplicates    → ALLOW if authorized
MEDIUM non-financial operational objects            → policy-dependent
HIGH   refund, cancellation                         → REQUIRE_APPROVAL, always
```

`DENY` and `REQUIRE_APPROVAL` cannot be overridden by the model, by repeated asking,
or by the frontend.

## Approval

The approve button is not the security boundary. On approval the server re-checks:
the approval record exists and is `PENDING`; the approver belongs to the same
merchant; the approval has not expired (15 min TTL); the full policy evaluation
passes *again*; and the payment's preconditions still hold (still captured, amount
still within refundable balance).

## Idempotency and the action record

`agent_actions` is the action-**attempt** record, distinct from `refunds` (the
resulting business entity). The order is mandatory:

```
INSERT agent_actions (status=PENDING, idempotency_key UNIQUE)   ← claims the action
        ↓
external call
        ↓
UPDATE  (status, external_reference)
        ↓
verification
```

The key is derived **server-side**:

```
sha256(merchant_id | external_payment_id | action_type | approval_id)
```

It is deliberately absent from the tool's input schema. A model-supplied key defeats
deduplication entirely: on retry the model emits a *fresh* key, the uniqueness check
misses, and the refund executes twice. Including `approval_id` means a separately
approved second refund is a genuinely distinct action, while any retry of the same
approved action collapses onto one key.

If the INSERT conflicts, the action was already attempted — the provider is not
called; the existing row is read and re-verified instead.

## Verification

> A successful API response is not proof of business state.

Verification re-reads the **payment**, not just the refund object. A refund can
legitimately sit at `pending`, so trusting `refund.status` alone would report the
ordinary path as ambiguous. `payment.amount_refunded` is the business fact.

```
SUCCESS  amount_refunded increased by the expected amount AND refund processed
PARTIAL  refund accepted (id issued) but the payment does not yet reflect it
FAILED   refund failed / rejected AND amount_refunded unchanged
UNKNOWN  the resulting state could not be read, or the effect cannot be attributed
```

The last clause matters. If a timeout occurs and the payment shows a refund but we
hold no external reference, we cannot attribute that refund to *this* action —
another process may have caused it. Reporting SUCCESS there would be exactly the
false verification the design forbids. It reports `UNKNOWN`.

## UNKNOWN is resolvable

`UNKNOWN` is a **pending safety state, not a verdict**. Left as a dead end it would
be an admission of defeat rather than a safety feature.

Resolution runs through the idempotency key. After a timeout we hold no external
reference, so the only way to learn whether the action landed is to ask the provider
about *our own key*:

```
timeout → reconcile by idempotency key
            ├─ provider reachable, refund found  → recover reference → SUCCESS
            ├─ provider reachable, no refund     → FAILED
            └─ provider unreachable              → remain UNKNOWN, retry later
```

`POST /tasks/{id}/reverify` exposes this, and the UI surfaces a Re-verify control on
every `UNKNOWN` result.

### Reconciliation sweep

Operator-driven resolution alone is insufficient: an action nobody looks at stays
unsettled forever, which defeats the purpose of detecting ambiguity at all.
`app/verification/reconciler.py` sweeps unsettled actions, and is a plain function
over the database rather than a worker — a queue would mean Redis/Celery, excluded
from this scope.

```
find UNKNOWN | PARTIAL, older than min_age, attempts < max
        │
        ▼
  reverify_action  (reconcile by idempotency key — a READ, never a retry)
        │
        ├─ settled   → update action + owning task
        └─ unsettled → attempt++; at the cap, ESCALATE to the operator queue
```

Three properties make it safe unattended:

- **Min-age guard (30s)** — a refund submitted seconds ago may not have propagated;
  burning an attempt on it can escalate a healthy action.
- **Bounded attempts (5)** — then escalate rather than sweep forever, so a genuinely
  stuck action becomes visible instead of being quietly re-polled. The CLI exits `2`
  so cron can alert.
- **Settlement is a read** — the sweep has no code path that issues a financial
  action. `test_sweep_settles_unknown_without_reissuing` asserts the refund row count
  is unchanged.

## Fault injection

Timeout and UNKNOWN scenarios cannot be produced by seeding data — they are
**adapter-level** faults. `FaultInjector` is a scenario-driven seam in the adapter,
inert unless a scenario enables it, with every injected fault recorded on the
`tool_calls` row so evaluation runs stay auditable.

`TIMEOUT_AFTER_SUBMIT` deliberately applies the state change *before* raising.
Modelling it as "raise before doing the work" would make it a safe no-op and would
never exercise the dangerous case UNKNOWN exists for.

## Replay

Two modes, never conflated:

- **PLAYBACK** — render the recorded trace. Deterministic by construction. Executes
  nothing.
- **RE_REASON** — re-run the agent against frozen tool results from the trace.

Safety does not rely on withholding tools. Two independent barriers make a financial
side effect unreachable: the runtime halts at `REQUIRE_APPROVAL` and never executes
(execution is only reachable via `approve_and_execute`, which replay never calls),
and `execute_read_tool` has no implementation for HIGH-risk tools. The replay
function asserts the outcome rather than trusting the design.

**Divergence is classified, not suppressed:**

- *Reasoning divergence* — a different tool sequence from identical frozen evidence.
  This is what replay consistency measures.
- *State divergence* — policy reached a different decision because the world changed
  (typically: the original refund now exists, so the duplicate guard correctly denies
  a second). That is the policy engine working, not the agent being inconsistent.

An earlier implementation withheld HIGH-risk tools during replay, which guaranteed a
*false* divergence on every action task and made the metric meaningless. That is why
the distinction exists.

## Failure model

Failures are classified, never collapsed into a generic error:

```
TOOL_TIMEOUT           TOOL_INVALID_ARGUMENT   TOOL_UNAVAILABLE
AUTHORIZATION_DENIED   POLICY_DENIED           APPROVAL_REJECTED
APPROVAL_EXPIRED       EXTERNAL_API_ERROR      EXTERNAL_STATE_UNKNOWN
VERIFICATION_FAILED    PARTIAL_EXECUTION       MODEL_INVALID_OUTPUT
EVIDENCE_INSUFFICIENT  BUDGET_EXCEEDED         REPLAY_DIVERGED
```

## Future state (not built)

Ordered in `docs/gap-closure-plan.md`. Nearest first: the remaining nine tools of §18 —
which is what makes five of the seven recovery interventions actionable; model-emitted
structured output (§37); the revenue-recovery ledger and dashboard (§49, §50).

Beyond that: specialised agents; Next.js frontend; Redis/Celery for durable retries;
per-merchant policy configuration; distributed tracing; containerised deployment.

None of it is justified until the loop above is reliable, and none of it is claimed
as implemented.
