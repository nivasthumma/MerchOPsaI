# ADR-0044 — State that must agree across replicas

**Status:** Accepted · 2026-09-05
**Phase 1 of the readiness review, item four.**

## Context

The readiness review named three pieces of process-local state. One of them was
a security control, one was a live operator switch, and the third should not
have been on the list.

**The rate-limit counter** (`app/api/security.py`) is the one that mattered.
Held in a module dict, it is exact with one worker and multiplies by N with N of
them: three API replicas serve three times the configured limit. On Vercel —
still a supported deployment — it is not approximate but absent, because every
invocation may be a new process and the counter starts empty on most requests.
The module said so honestly ("with several it is approximate"), and honesty
about a control that is not enforced is not the same as enforcing it.

**The runtime provider override** (`POST /config/llm-provider`) is a live switch
between configured providers. In a module global it applied to whichever replica
served the POST. An operator switching to the deterministic planner would get a
200, see the change reflected in the response, and watch the model keep being
used by the other two replicas — with the metrics afterwards explained by
nothing.

**The credential-detection cache** was on that list and should not have been. It
is `@lru_cache` over a probe of the process's own environment; every replica has
the same environment, would reach the same answer, and changing that environment
means a restart anyway. It is process-local and correct. The earlier review
overstated it, and this ADR says so rather than quietly dropping it.

## Decision

`app/shared_state.py`, behind `REDIS_URL`.

### The window slides, and the clock is Redis's

The in-process limiter has always been a **sliding window log** — it keeps
timestamps and drops the ones past a cutoff. Its module docstring called it a
fixed window for a long time; the code never was one. That docstring is now
corrected, and the shared implementation preserves the sliding behaviour rather
than simplifying to a fixed window, because a fixed window lets a caller send
twice the limit across a boundary. Changing a security control's algorithm while
moving where it is stored would be two changes wearing one commit.

It runs as a Lua script over a sorted set, so drop-expired, count, and add are
one atomic step. The script reads `TIME` **inside Redis** rather than taking a
timestamp from the caller. A timestamp from the caller is that replica's clock,
and the entire point is that several replicas share one window — two servers a
second apart would otherwise disagree about what "the last sixty seconds"
contains. Redis's clock is the only one all of them can see.

### Degrading is a designed state, not a failure

`REDIS_URL` unset is supported, not broken: a laptop and a single-container
stack are both single-process, where the in-process implementation is exact.

When Redis is *configured* and stops answering, `consume` returns `None` and the
caller applies its own per-process limiter. Failing closed would turn a Redis
blip into an outage of the whole API. Failing open would remove a security
control silently. The fallback lands on the documented single-process behaviour
— a known state rather than a new one — and `/health` reports
`shared_state.backend` as `shared`, `process` or **`degraded`**, so a deployment
that believes it is sharing state can discover that it is not.

Socket timeouts are 250 ms. This sits in front of every authenticated request; a
Redis that has stopped answering must cost milliseconds and fall back, not hold
requests open.

### `UNAVAILABLE` is not `None`

`get_provider_override` is three-valued. `None` means "no override is set",
which is a real answer. A caller that could not tell that apart from "I could
not ask" would treat a Redis outage as somebody having cleared the override —
switching a fleet back to its configured provider in the middle of whatever the
override was set for. The unreachable case falls through to this process's own
last-known value instead.

### The response says where the switch landed

`POST /config/llm-provider` returns `applies_to: fleet` or `this_replica_only`,
and audits the same field. An operator who changed the provider everywhere and
one who changed it on a third of their fleet should not receive the same
response, and afterwards the audit log should explain the metrics.

## Consequences

**One defect in this work was found by its own tests.** The registered Lua
`Script` object holds the client it was registered against, and the reset hook
did not clear it — so a test that repointed `REDIS_URL` ran against the previous
client. The symptom is the dangerous shape: a rate limiter that appears to work
while pointed at nothing. Cleared with the client now.

**CI runs the shared tests rather than skipping them,** via a `redis` service
and `TEST_REDIS_URL`. Deliberately *not* `REDIS_URL`: setting that would put the
whole suite on the shared limiter, and the per-process path is what most
deployments run and what every other test assumes. The container job separately
asserts the compose stack comes up reporting `all_replicas`, because an API that
comes up reporting `this_replica_only` with Redis wired means the client could
not reach it — which looks fine until there are two replicas.

**Redis persists nothing,** and the compose service says so with `--save ""
--appendonly no`. A rate limit carried across a restart would apply a caller's
old refusals to a fresh process; the provider override deliberately does not
survive one.

## What this does not do

**It does not make the API horizontally scalable on its own.** It removes the
reason the rate limiter was wrong with more than one replica. What is still
single-process is the agent run itself: a task executes inside the request that
created it, so a long investigation occupies a worker for its duration. The
shape that fixes it — 202 and a poll — is the next item.

**It adds a dependency that can be down.** That is why the fallback exists and
why `degraded` is reported rather than logged and forgotten. A deployment that
wants the limit enforced strictly should alert on `shared_state.backend`, which
is now possible; before this there was nothing to alert on.
