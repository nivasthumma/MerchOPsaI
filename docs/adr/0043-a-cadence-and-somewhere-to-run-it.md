# ADR-0043 — A cadence, and somewhere to run it

**Status:** Accepted · 2026-09-05
**Phase 1 of the readiness review, items two and three.**

## Context

Four things in this system were built to run repeatedly and had nothing to run
them.

| | what it settles | how it ran before |
|---|---|---|
| `drain` | events sit `PENDING` in `event_outbox` until delivered — and every notification consumer hangs off that delivery | `POST /events/drain`, by hand |
| `notify` | an approval expiring is the *absence* of a decision, so no event fires | ADR-0042 shipped it with no caller |
| `reconcile` | an action left UNKNOWN settles when its provider state is re-read | `make reconcile`, by hand |
| `detect` | incidents are found by a sweep over payment history | `POST /incidents/detect`, by hand |

The README named this for detection and reconciliation and was right to. What it
did not say is that the same gap made the notification work of ADR-0042 inert
for two of its six kinds: an approval could be chased only if somebody
remembered to run the sweep, which is the problem that ADR existed to remove.

There was also nowhere to run a worker even if one existed. The only documented
deployment was Vercel — serverless, one entrypoint, sixty-second ceiling, no
long-lived process by construction. No Dockerfile, no compose file, no
infrastructure of any kind.

## Decision

### `app/worker.py` — one loop, four jobs

Sequential and single-threaded. The drain claims rows `FOR UPDATE SKIP LOCKED`
and notification sends deduplicate on a UNIQUE constraint, so a *second worker*
is safe; within one worker there is no value in concurrency and considerable
value in a log that reads top to bottom.

Three properties are the reason this is a loop and not four cron lines, and each
is tested:

- **A failing job never stops the others.** An exception is logged with a
  consecutive-failure count and the loop continues.
- **A failing job never spins.** The schedule advances *after* the run and
  *regardless of outcome*. Without that, a job failing in a millisecond is due
  again immediately and starves everything behind it.
- **SIGTERM finishes the current job.** The signal sets a flag checked between
  jobs. Stopping mid-drain would leave events claimed by a transaction that
  never commits.

`--once` runs each job once and exits, so a platform scheduler can drive exactly
the same code. This is not a replacement for cron; it is one process to run
beside the API with no second system to configure, and it makes the intervals a
property of the application rather than of whichever host it landed on.

Detection is called once per merchant, in a loop, never as a cross-merchant
query — `detect` takes a merchant by argument "never by discovery"
(MerchantOps §54). Enumerating merchants is the worker's business; the read
stays scoped.

### One image, two entrypoints

`api` and `worker` are the same build with different commands, because they are
the same code differing only in what they call. Building them separately would
mean two things to keep in step and one of them drifting.

**Migrations are a separate one-shot service, not an entrypoint step.** An image
that migrates on boot races itself the moment there is more than one replica,
and turns a schema change into something that happens implicitly during a
rollout rather than a step somebody ran and watched.

**The runtime dependency set is its own lock.** `requirements-runtime.txt` is 36
packages against the development set's 66. Streamlit is the difference that
matters — pandas, numpy, pyarrow and altair, hundreds of megabytes in every
layer, pull and cold start, to ship an MVP UI the React SPA replaced. `pytest`
is the other. The image is 79 MB, non-root at uid 10001, installed with
`--require-hashes`, and carries `curl` and nothing else beyond the runtime.

The SPA is built inside the image from source, so an image cannot ship a stale
`web/dist` somebody forgot to rebuild — and `npm run build` runs `tsc` first, so
a type error fails the image build.

### `/ready`, which is not `/health`

`/health` reports the process's posture and answers without touching anything.
That makes it a liveness probe: if it returns, the process is alive.

It is the wrong thing to route traffic on. A container whose database is
unreachable, or whose schema is behind the code deployed over it, passes
`/health` and cannot serve a request — and the 500s that follow look like an
application fault. `/ready` asks the two questions that decide whether an
instance can do work, and returns 503 when either fails.

The two failures are reported **separately**, which is the part worth keeping:
`database.ok: false` means the database is down; `schema.ok: false,
error: "unstamped"` means run the migrations. Different remedies, different
people, and a probe that cannot tell them apart sends whoever is paged to the
wrong place. An early version conflated them — a missing `alembic_version` table
raised inside the same `try` as the connection check and was reported as an
unreachable database.

## Consequences

**A promise in a comment is now implemented.** `app/config.py` said
`notify_approval_warning_seconds` was "asserted at startup" against
`approval_ttl_seconds`. It was not. `check_configuration()` now raises for that
and for an unrecognised `NOTIFY_MIN_SEVERITY`, both at import, because their
only symptom is a notification arriving at the wrong moment or not at all —
silent by construction. This is the same defect shape as the secret guard in
`e9577e3`: a docstring describing a control nobody wrote.

**The published database port is 5433, and configurable.** Anyone running this
already has PostgreSQL on 5432 as often as not, and `docker compose up` failing
to bind is a poor first experience of a tool meant to remove that class of
problem. `DB_PORT` and `API_PORT` override.

**CI builds the image and brings the stack up.** A Dockerfile nothing builds is
a Dockerfile that stops working, and the way that gets discovered is during an
incident. The job also asserts `/ready` is true and that the worker logs a
completed job — building proves it compiles, which is a different claim from
proving it runs.

**All three deployment dependency sets are now hash-pinned**, including
`api/requirements.txt`, which was still on `>=` floors after the Phase 0 work.

## Verified

The stack was brought up and exercised end to end, in containers, with nothing
driving it:

- `migrate` ran to head and confirmed the audit-immutability triggers fire
- `/api/ready` reported `at: 351d7cd7c50d, expected: 351d7cd7c50d`
- the SPA served, and a deep link fell back to `index.html`
- the worker opened **3 incidents** across 3 merchants on its own
- a task posted to the API raised an approval, the worker drained it, and **both
  approvers were notified** — the chain that did not exist a day ago
- an approval moved to two minutes from expiry was **chased at CRITICAL
  severity** by the worker's next notify sweep

Those three LOW/MEDIUM incidents produced no notification, which is correct: the
floor is HIGH. The events show `PUBLISHED`, so the consumer ran and declined.

## What this does not do

**No horizontal scale yet.** A second worker is safe by construction, but the
API still holds three pieces of correctness-relevant state in process memory —
the rate-limit counter, the runtime provider override, the credential-detection
cache. Redis is the next item.

**No agent-run asynchrony.** A task still runs inside the request. The
sixty-second ceiling is gone with Vercel, but the shape that makes a long
investigation possible — 202 and a poll — is not built.

**This is not a production deployment.** It is the shape of one, on one host,
with a known database password and no TLS. A managed database with backups, a
secret store, TLS termination, a registry with signed tags and resource limits
are choices about somebody's infrastructure, and none of them is here.
