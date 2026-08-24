# Architecture

## Context and problem

A merchant operations team needs to answer questions like "why did revenue drop this
week?" and "did we double-charge this customer?" — and then *act* on the answer. The
acting part is where AI systems usually become unsafe: a language model that can call
a refund API is one hallucination away from moving real money.

The design premise here is that **the model is a reasoning component inside a
deterministic system**, not the system's authority.

## The core loop

```
Merchant request
    → intent + bounded plan
    → read-only investigation tools
    → evidence synthesis (typed Findings)
    → recommendation
    → risk classification        ← property of the TOOL, not of model output
    → policy decision            ← deterministic, outside the model
    → human approval (HIGH risk) ← server-side, expiring
    → action via typed tool      ← idempotency reserved BEFORE the call
    → independent verification   ← reads back business state
    → audit trace
    → replay
```

## Request path in detail

Everything that matters happens between the model's tool request and the tool running:

```
model emits tool_use
        │
        ▼
  1. Is the tool registered?              unregistered → TOOL_UNAVAILABLE
        │
        ▼
  2. Argument validation                  invalid → TOOL_INVALID_ARGUMENT
        │                                 (MUST precede step 3 — see below)
        ▼
  3. Policy engine
        ├─ permission check               → DENY missing_permission
        ├─ merchant ownership             → DENY merchant_isolation
        ├─ risk classification
        ├─ amount / balance limits        → DENY amount_limit_exceeded
        ├─ duplicate-action guard         → DENY duplicate_action
        └─ HIGH risk                      → REQUIRE_APPROVAL (loop halts)
        │
        ▼
  4. Execute (LOW risk only from the loop)
        │
        ▼
  5. Persist tool_call + audit events
```

**Why argument validation precedes policy.** The policy engine queries the database
using the tool's arguments (payment ownership, refundable balance). Passing an
unvalidated model-supplied value into that query is both a crash and an injection
surface. This ordering was not in the original contract; it was found by scenario
SEC-04, which passed `synthetic_payment_id: 12345` (an integer) and produced a
`ProgrammingError` from PostgreSQL. The fix is recorded here because the ordering is
load-bearing, not incidental.

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

Specialised agents; Next.js frontend; Redis/Celery for background reconciliation and
durable retries; 100+ scenario benchmark wired into CI; per-merchant policy
configuration; distributed tracing; containerised deployment.

None of it is justified until the loop above is reliable, and none of it is claimed
as implemented.
