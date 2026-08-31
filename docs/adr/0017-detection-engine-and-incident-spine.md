# ADR 0017 — The detection engine and the incident spine

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §11, §12, §13, §22, §38, §47, §58

## Context

The build began at "a user asks a question" (`POST /tasks`). MerchantOps §64's vertical
slice begins three boxes earlier, at `event → detection → incident`. Without that spine,
§48's operating loop, §50's dashboard, §23's recovery planner and §51's incident page all
have nothing to attach to, and §73's claim — that this is not a chatbot connected to
Razorpay — is unsupported by the code.

This ADR records the decisions taken building it.

## 1. Detection reads `payments`; there is no `events` table yet

`docs/gap-closure-plan.md` put an `events` table (§11's durable event store) in this
phase. It is deliberately **not** built here.

In this system the synthetic dataset *is* the observed history: a `payments` row with a
`created_at` is the event. An `events` table would have no producer until webhook
ingestion exists, and a table nothing writes to is the "collection of disconnected
skeleton components" both specs explicitly warn against (MerchantOps §63). It lands in
the webhook phase, where it has a writer.

The cost of deferring is honest and small: detection currently observes state rather than
a stream, so it cannot see an event that never changed a payment row. Nothing in the
current rule set needs one.

## 2. Lifecycle transitions are written out, not derived from an ordering

The obvious implementation of §13 is one line: legal iff the target sits later in the
canonical chain. That was the first draft and it is wrong in the place this project cares
about most.

`UNKNOWN` occupies no position in a linear order. An incident parked in UNKNOWN because
its actions have not settled must be able to reach `RESOLVED` once reconciliation settles
them. An ordering rule either forbids that — stranding the incident, which is precisely
the failure `UNKNOWN` exists to prevent (§33) — or admits backward moves generally, in
which case it forbids nothing at all.

So `app/incidents/lifecycle.py` writes the map out. It is longer and it is checkable by
reading. Forward skips are legal (an incident whose recommendation is NO_ACTION never
reaches `EXECUTING` and must still resolve); backward moves are not; `CLOSED` is the only
truly terminal state.

## 3. Detection is idempotent through a derived key

`Incident.detection_key` is UNIQUE and every rule derives it from the facts of the anomaly
— merchant, type, method, window start — never from the clock. Re-running the sweep
re-derives the same key and the second insert collides.

This is deliberately the same mechanism `agent_actions.idempotency_key` uses to make
duplicate *execution* impossible, applied to duplicate *observation*. The insert is
attempted rather than pre-checked: a SELECT-then-INSERT is a race two concurrent sweeps
both lose, so the unique constraint is the authority and the collision is the check.

An operations console that grows a fresh HIGH incident on every detection pass is worse
than no console.

## 4. Incident status follows task status, never model prose

`app/incidents/manager.py` exists to hold one invariant:

    task status  -> incident status     (deterministic)
    model prose  -> incident status     (never)

An agent that concludes "this is resolved" resolves nothing. The input to the lifecycle
move is the task's recorded status, which the runtime sets from observable behaviour —
tools called, policy decisions returned, budgets exhausted. This is §38's separation of
agent state from financial state, applied to the incident, and it is covered by both a
test using a provider whose prose claims resolution and a mutant that resolves regardless
of outcome.

Investigation stops at `ROOT_CAUSE_IDENTIFIED`. Everything past it belongs to the recovery
planner (§23), which is not built. The single exception is an investigation that reaches a
policy decision requiring approval: those intermediate states are recorded because policy
genuinely evaluated and genuinely required a human, not to make the chain look complete.

## 5. The task is bound to its incident at creation

Found by a test, and worth recording because it is unfixable after the fact.

`app.audit.trace.record` reads `incident_id` off the task as each event is written, and
audit rows are immutable by database trigger (`scripts/harden_db.py`). A task bound to its
incident *after* the run therefore leaves every event of that run off the incident's trace
— permanently, with no backfill available. `AgentRuntime.run` now takes `incident_id` and
sets it on the `AgentTask` row at creation.

This is what makes §58's single incident-rooted ordering — detection, every lifecycle
move, and every event of every task the incident dispatched — actually reachable.

## 6. Revenue at risk is computed, and reproducible from its own evidence

§22 gives the formula: `(expected successes − actual successes) × expected transaction
value`. `expected` uses the method's **own** baseline rate over the volume actually
attempted; a global rate would attribute one method's shortfall to another's traffic.

The figure is written by deterministic code and is never model output. The signals that
produced it are published on the incident, and a test asserts an operator recomputing from
those published signals lands on the displayed figure to within 0.1% — rates are shown to
one decimal place, so agreement is to display precision. A financial number nobody can
reproduce from its own evidence is a number taken on trust, which is what §22 avoids.

## 7. Hourly onset needs a wider margin than the rule that found the incident

A bug caught in review, recorded because the reasoning generalises.

Onset detection works on hour buckets holding a dozen attempts each, not the hundreds the
method-level rule sees. At that size ordinary variance clears the method-level threshold
easily — two failures in a twelve-attempt bucket is an 11-point "drop" and means nothing.
The first implementation reported the first *noisy* hour in the window as the incident's
start, which would have opened §51's timeline with a time the problem had not started.

Hour buckets now need both a minimum volume and twice the method-level margin. A scenario
(`DET-09`) and a mutant cover it, so it is a graded failure rather than a unit-test-only
catch.

## Consequences

- The operating loop now runs `payments → detection → incident → investigation → lifecycle
  → audit`. Recovery, execution and measurement remain unbuilt (§23, §27, §49).
- Two entry points now exist for the agent: a user question and an incident dispatch. They
  converge on the same runtime, the same policy engine and the same audit trail — the
  incident supplies context (§20), not authority.
- `Incident.severity` is deterministic and derived from exposure and drop size. On the
  seeded dataset the headline UPI incident grades MEDIUM, not the HIGH of MerchantOps
  §6's illustration. The thresholds were not tuned to make the demo read better.
