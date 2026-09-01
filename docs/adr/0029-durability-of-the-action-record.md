# ADR 0029 — The record outlives the request that made it

**Status:** Accepted · 2026-09-01

## Context
The architecture states the order in which a financial action is performed:

```
INSERT agent_actions (status=PENDING, idempotency_key UNIQUE)   ← claims the action
        ↓
external call
```

That was not what executed. `app/db.py` held the only `commit()` in the codebase,
inside `session_scope`, so an entire request — the task row, every tool call, every
audit event, and the action claim — was one transaction that committed at the end.
Everything in between was `flush()`.

Three consequences, none of them intended:

1. **The claim was not durable when it mattered.** A flush is not a commit. If the
   process died after Razorpay accepted a refund — an exception on the way back, a
   dropped connection, the host killing the invocation — the transaction rolled back
   and took the claim with it. The money had moved and nothing on our side said so:
   exactly the state `agent_actions` exists to prevent.

2. **An unclassified failure erased its own evidence.** `app/agent/runtime.py`
   contained no `except` at all. A provider that raised propagated to `session_scope`,
   which rolled back and destroyed the run's whole trace. "Every exit is a recorded
   outcome, not a silent drop" held for the failures the system anticipates and for no
   others — `INTERNAL_ERROR` was in the taxonomy with no path to disk.

3. **The budget was not the budget.** `max_wall_clock_seconds` was 60 and
   `vercel.json` set `maxDuration: 30`. Every task that used its documented allowance
   was killed by the host at half of it, mid-transaction — and the wall clock was
   checked only *between* turns, with no timeout on the model call, so one hung
   request could hold a transaction open for as long as the transport allowed.

## Decision
Four changes, one property: what happened is recorded, and the record survives.

**`db.checkpoint(session)`** commits before an irreversible external call. Used at
both reservation sites — the refund in `app/tools/actions.py` and the shared
`_reserve` behind payment links and notifications. Everything committed alongside the
claim is history that already happened; none of it should be undone by what comes
next.

**A failure boundary in `AgentRuntime.run`.** An unhandled error marks the task
FAILED / `INTERNAL_ERROR`, records a redacted `task_crashed` event, commits, and
re-raises `AgentRuntimeError` carrying the task id. One `@app.exception_handler`
turns that into a 500 naming the task, so every endpoint that starts a run fails the
same way.

**The budget is held inside the host's timeout.** `Settings.effective_wall_clock_seconds`
caps the configured budget at `platform_timeout_seconds` less a margin, and the loop
passes its remaining time to `provider.turn()` so a single call cannot outlive the
run. `/health` publishes configured and enforced side by side.

A deadline handed to the SDK bounds one HTTP *attempt*, not the call: with the default
two retries, a 30s timeout permits ninety seconds of waiting. So the provider divides
whatever deadline it is given across the attempts that may be made against it. The
retries are kept — §57 grades a provider 5xx `BOUNDED_BACKOFF`, and absorbing one is
correct behaviour, not a convenience — but they can no longer be the way a turn
outlives the budget that bounds it.

**The sweep picks up abandoned claims.** `find_unsettled` now also returns actions
that are PENDING with no verification — a claim whose request died before recording
an outcome.

## Rationale
**Why commit mid-request rather than shorten the transaction.** The alternative was a
separate connection for the claim, which is the textbook answer and the wrong one
here: it splits the action record from the audit events that explain it, and it
breaks the test suite's isolation model. Committing forward is honest — the rows
being committed describe things that already occurred, and there is no case where the
right response to a later failure is to pretend an earlier one did not happen.

**Why the test suite did not have to change.** `tests/conftest.py` already binds the
session factory with `join_transaction_mode="create_savepoint"`, precisely so
application code may commit. A `commit()` releases a SAVEPOINT and each test still
rolls back to nothing. The infrastructure for this decision was built before the
decision was made.

**Why an abandoned claim is a reconciliation case and not a new one.** It holds an
idempotency key and no outcome, which is the definition of "we may have called the
provider and do not know" — `UNKNOWN` in everything but the column. It gets its own,
wider age guard: `min_age_seconds` waits out the *provider*, but this waits out *us*,
and a row still PENDING may belong to a request that is mid-call and about to write
the state a sweep would be overwriting. The guard is derived from the enforced budget
rather than guessed.

**Why the crash detail is audited and not returned.** The message is whatever the
raising library chose to put in it, and provider errors quote the request they failed
on. It goes through `redact` into the audit trail, where access is controlled. The
response carries the task id, the failure classification, and nothing else.

## Consequences
- A refund can no longer exist at the provider with no record of who caused it.
- A crashed run is inspectable: the task, its tool calls, its messages and its audit
  events are all still there, ending in `task_crashed`.
- `trace_preserved: false` is possible and reported — when the session itself was
  unusable there is no trace, and saying so beats pointing an operator at a task id
  that resolves to nothing.
- A rolled-back request now leaves a PENDING claim behind on purpose. That is a state
  the reconciler settles by key, and `GET /actions/escalated` lists it if it cannot.
- The correlation id moved from a module global to a `ContextVar` in the same change
  (`app/audit/trace.py`). The global's premise — one run per process at a time — is
  false: FastAPI runs every `def` endpoint in a threadpool, so concurrent runs were
  overwriting each other's id and joining unrelated traces.
- `maxDuration` rises to 60 to match the budget; `PLATFORM_TIMEOUT_SECONDS` mirrors it
  in configuration. The two must be changed together, and `config.py` says so.
- 11 tests in `tests/integration/test_durability.py`.
