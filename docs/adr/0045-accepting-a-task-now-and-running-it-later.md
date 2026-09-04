# ADR-0045 — Accepting a task now, running it later

**Status:** Accepted · 2026-09-05
**Phase 1 of the readiness review, item five. Completes Phase 1.**

## Context

A task executed inside the request that created it. On a serverless host that
is the only shape available, and it is why `max_wall_clock_seconds` was capped
by the platform's own timeout — a budget larger than the host's is not a budget,
because the invocation is killed part-way and the careful `ABORTED_BUDGET` path
never runs.

Everywhere else it is the wrong shape. An investigation holds an HTTP connection
and an API process for its whole duration, the budget has to be small enough
that no proxy in the path gives up first, and a second API replica does not help
because each request still occupies the replica that received it. After ADR-0043
gave the system a worker and ADR-0044 shared the state that must agree, this was
the last thing making a second replica awkward.

## Decision

`POST /tasks` takes a mode. `inline` runs the loop in the request and returns
the finished task — unchanged behaviour, and the only thing possible without a
worker. `async` writes the task, returns **202** with its id, and lets a worker
run it; `GET /tasks/{id}` is the poll.

Default `inline`, so nothing that works today stops working. The compose stack
sets `async` because it has a worker. `?mode=` overrides per request.

**None of this touches the evaluation suite.** The 187 scenarios drive
`AgentRuntime` directly rather than through HTTP, so the numbers are comparable
across this change by construction rather than by assertion.

### Provenance is written when the task runs, not when it is accepted

`model_version`, `model_provider` and `prompt_version` become nullable, and are
null exactly while a task is QUEUED. §41 records *what ran*; a queued task has
not run, and the provider can genuinely change in between — `POST
/config/llm-provider` exists to do that, and after ADR-0044 it changes it for
the whole fleet. A model version written at enqueue would be a prediction stored
where a measurement belongs, which is the one thing §41 exists to prevent.

### An abandoned run is failed, not retried

A worker killed mid-run leaves a task RUNNING with a lease nobody renews. The
obvious move is to put it back on the queue. This does not, and the reason is
money: a run that got far enough to execute an action has already contacted a
payment provider, and re-running it re-enters a loop whose earlier steps are
invisible to the second attempt. Idempotency keys stop *the same action* from
executing twice; they do not make replaying half an investigation safe.

So it becomes FAILED with `WORKER_LOST` and a person decides. That is the same
posture the rest of this system takes toward an outcome it cannot establish.

The lease is derived from the execution budget rather than picked — three times
the enforced wall clock, floor 300s — so a task using its whole allowance is
never mistaken for an abandoned one. AWAITING_APPROVAL is not RUNNING, so a task
waiting on a human is never touched however long it waits; that is what makes it
a separate state.

### Authority is read when it is used

The worker rebuilds the principal from the task's user row, never from anything
carried at submission. A task queued an hour ago must not run with permissions
its submitter has since lost — the same rule `current_principal` follows by
re-reading permissions from the database on every request. Tested by removing
the permission after enqueue and asserting the run sees the removal.

### A worker says it is alive, and the absence is visible

`worker_heartbeats` is one upserted row per worker. Everything cadence-driven
runs in that process, and until this table existed the absence of work looked
exactly like there being no work to do — the dead-man's switch the README asked
for in limitation 18.

It also lets `POST /tasks` **refuse** an asynchronous submission when no worker
has reported recently. Returning 202 for a task that will never start would be
the notification problem again: a queue nobody is draining looks identical to a
queue with nothing in it. 503 says which.

`/health` reports queue depth, the age of the oldest queued task, and when a
worker was last seen. Depth alone cannot say a queue is stuck — a deep queue
that is moving is healthy and one task that has waited an hour is not.

## Consequences

**Two defects were caught by existing guards, which is what they are for.**

The schema drift guard rejected the partial indexes: I wrote them in the
migration and not on the model, and `test_head_matches_the_models` compares the
schema at head against `Base.metadata`. That is precisely the disagreement it
exists to catch, and it caught it on the first run.

The frontend contract check rejected `QUEUED`: `web/src/api/types.ts` carries a
hand-written `TaskStatus` union that `contract.ts` compares against the
generated schema at compile time. Its own comment records that the same check
previously caught `PENDING` and `DENIED` missing. It has now caught a third.

**One defect was mine and had no guard.** The generated migration added
`attempts` as `NOT NULL` with no `server_default`, which fails on any table that
already has rows. Fixed, and verified by rewinding a database that had a task
row and upgrading it.

**The client polls without knowing which mode ran.** `api.awaitTask` returns
immediately for a task that is already terminal and polls otherwise, so the page
does not need to know how the server was configured. It is bounded: an unbounded
poll against a queue nobody is draining is a spinner that never stops and never
says why, so it gives up and hands back the task in whatever state it reached.
AWAITING_APPROVAL counts as terminal — it is waiting for a person, and the page
has something to show.

## What this does not do

**No priority, no fairness, no per-tenant queue.** One FIFO queue, oldest first.
A tenant submitting a hundred tasks delays everyone behind them. That is
acceptable at this size and will not be at the next one; the fix is a queue key
per merchant and round-robin claiming, and it is not built.

**No cancellation.** A queued task can be neither cancelled nor de-prioritised,
and a running one cannot be stopped short of its budget.

**Concurrency is more workers, not more threads.** One worker claims and runs
one task at a time, bounded per pass so a busy queue cannot starve the sweeps.
Two workers claim different rows with nothing further required — but nothing
here tunes how many to run, and there is no autoscaling signal beyond the queue
depth now reported on `/health`.
