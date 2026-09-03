# ADR 0031 — Two channels: what was decided, and what is happening

**Status:** Accepted · 2026-09-01

## Context
`grep -rn 'import logging' app/` returned nothing. Not sparse logging — none.

The audit trail carried everything, and it is very good at what it is for:
`audit_logs` is durable, immutable by database trigger, scoped per tenant, and answers
what the system *decided* and on what evidence. That is domain evidence, and it is the
right shape for it.

It is no help operationally. It cannot say that a request returned 500, that a query
took four seconds, that the connection pool is exhausted, or what the p99 is. The
failures it is worst at are the ones that matter most:

- A request that fails before reaching the runtime was never a task, so it left no row.
- Before ADR-0029, a run that died mid-transaction rolled its own evidence away.
- After ADR-0029 there is a `task_crashed` row — behind authentication, in one
  tenant's data, which is not where the person who has to fix it is looking.

`/metrics` existed and is business counts per merchant: gated, approved, moved_minor.
Useful, and not the question a scraper asks.

## Decision
Two channels, kept apart, joined by `correlation_id`.

```
audit_logs  ->  what the system decided    durable, immutable, per tenant
stdout      ->  what the process is doing  ephemeral, operational, ordered
```

**Structured JSON to stdout.** Every platform this can run on ingests stdout — Vercel,
CloudWatch, Datadog, `journalctl`, a terminal. Writing anywhere else means choosing a
vendor at the logging layer, which is the wrong place to choose one. Every line carries
the correlation id without any call site passing it, because it is read from the
context at format time.

**One middleware, not sixty decorators.** `ObservabilityMiddleware` gives every request
a correlation id, one log line with its outcome, and a metric. Per-endpoint
instrumentation would be sixty chances to forget one, and the forgotten ones would be
the endpoints nobody thinks about — which are the ones that break.

**Runtime metrics separate from business metrics**, at `/metrics/prometheus`. No
tenant scoping, because a latency histogram does not belong to a merchant.

**Query timing in the engine**, so the sweep and the scripts are instrumented, not just
the API. Those are where a query quietly stops being cheap.

## Rationale
**Why no client library and no OpenTelemetry.** Prometheus' exposition format is a
documented line protocol and emitting it is less code than the adapter around a library
would be. `api/requirements.txt` already explains why pytest and Streamlit are kept out
of the function bundle; a metrics client and an OTel SDK are a poor way to spend the
same budget. This is a real trade: distributed tracing is the right answer eventually,
and `correlation_id` is deliberately the shape that makes adding it mechanical rather
than a rewrite. It is not claimed as done.

**Why the scrape endpoint authenticates with a shared secret and 404s when unset.** A
scraper has no merchant and must not be given one, so falling back to an ordinary
principal would mean minting a user for a robot. Unset, the route is absent rather than
open: metrics publish route names, traffic shape and error rates — a smaller leak than
data, and still a leak. This project already has one unauthenticated route that should
not be; it did not need a second.

**Why label cardinality is treated as correctness, not tidiness.** A label with
unbounded values is a permanent time series per distinct value. `/tasks/TASK_9F2A31C0`
recorded raw is one series per task, forever — a monitoring system that runs the
process out of memory, which is an outage *caused by* monitoring. The middleware
records the route template, an unrouted path becomes `<unmatched>` rather than whatever
the caller typed, and the registry caps distinct label sets and folds the rest into
`__overflow__`.

**Why the correlation id nests instead of being set and cleared.** The middleware sets
one at the HTTP boundary and a run sets its own inside it. A run that finished by
clearing the value would leave the rest of the request — its status, its duration —
logged as belonging to no trace, which is worse than the leak clearing was meant to
prevent. `correlation_scope` restores what was there. A run started by a request now
*inherits* that request's id, so the log line for the response and the audit rows the
run wrote carry the same id.

**Why redaction is shared with the audit trail.** One rule, one place. A secret that
must not reach the database must not reach stdout either, and stdout is by far the
easier of the two to forward somewhere nobody audited. Exception logging records the
type and message, never the frames: a stack trace on stdout is how a connection string
ends up in a log aggregator.

## Consequences
- **No new dependencies.** Everything here is stdlib, Starlette and SQLAlchemy. The
  function bundle is unchanged.
- Counters are per instance and in memory. That is how Prometheus expects to scrape a
  multi-instance service, and it is close to useless on Vercel, where an instance does
  not outlive the burst. `/metrics/prometheus` says so in the response body rather than
  leaving a dashboard quietly wrong.
- `alembic/env.py` deliberately does not call `fileConfig()`. Alembic's default
  reconfigures logging process-wide from an ini file and tears down existing handlers;
  in `scripts/migrate.py`, which runs migrations in-process, that silently replaced the
  application's own logging. Found because it broke a test, and it would have been
  invisible in production.
- Uvicorn's access log is disabled: it duplicates the middleware's line in a format
  carrying no correlation id.
- 21 tests in `tests/integration/test_telemetry.py`, kept separate from
  `test_observability.py`, which covers the audit trail. The two files are the two
  channels.
