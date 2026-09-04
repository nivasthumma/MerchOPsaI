# Running it in containers

```bash
docker compose up -d --build
open http://localhost:8000
```

Three processes and one image. The API and the worker are the same build with
different commands, because they are the same code differing only in what they
call; building them separately would mean two things to keep in step and one of
them drifting.

| service | what it is | notes |
|---|---|---|
| `db` | PostgreSQL 16 | published on **5433**, not 5432 — see below |
| `redis` | shared state | rate limit + provider override; no persistence, on purpose |
| `migrate` | one-shot | runs to completion; `api` and `worker` wait on it |
| `api` | `uvicorn api.index:app` | the SPA and the API under `/api` |
| `worker` | `python -m app.worker` | drain · notify · reconcile · detect |

## Why migrations are a separate service

Not an entrypoint step. An image that migrates on boot races itself the moment
there is more than one replica, and turns a schema change into something that
happens implicitly during a rollout rather than a step somebody ran and watched.
`migrate` runs `scripts/migrate.py`, which handles all four states a database
can be in — empty, unstamped-legacy, built-from-models, already stamped — and
which ends by checking that the audit-log immutability triggers actually fire,
exiting non-zero if they do not. That is what makes it a gate rather than a
formality.

## Why the database is on 5433

Because you probably already have PostgreSQL on 5432 — a system install, or
another project's stack — and `docker compose up` failing to bind a port is a
poor first experience of a tool meant to remove that class of problem. To point
the host test suite at the containerised database:

```bash
export DATABASE_URL=postgresql+psycopg2://merchantops:merchantops@127.0.0.1:5433/merchantops
```

## The health checks

`/health` and `/ready` answer different questions and the container uses the
second one.

`/health` reports the process's posture — which provider, which payment adapter,
what budget is actually enforced — and answers without touching anything. That
makes it a **liveness** check: if it returns, the process is alive and should not
be restarted.

`/ready` asks whether this instance can do work: can it reach the database, and
is the schema at the revision this code expects. It returns **503** when either
fails, because an orchestrator reads the status code. A container whose database
is unreachable, or whose schema is behind the code deployed over it, passes
`/health` and cannot serve a request; routing traffic to it produces 500s that
look like an application fault.

The two failures are reported separately, and that distinction is the point:
`{"database": {"ok": false}}` means the database is down, and
`{"schema": {"ok": false, "error": "unstamped"}}` means run the migrations.
Different remedies, different people.

```bash
curl -s localhost:8000/api/ready | jq
{"ready": true, "checks": {"database": {"ok": true},
 "schema": {"ok": true, "at": "351d7cd7c50d", "expected": "351d7cd7c50d"}}}
```

## What the image contains, and what it does not

Built from `requirements-runtime.txt` — 36 packages, not the 66 in
`requirements.txt`. Streamlit is the difference that matters: it pulls pandas,
numpy, pyarrow and altair, hundreds of megabytes into every layer, pull and cold
start, to ship an MVP UI the React SPA replaced (ADR-0015). `pytest` is the
other; shipping a test runner to production widens the attack surface and slows
the start for nothing. The result is **79 MB**.

Installed with `--require-hashes`, so a build either matches the lock exactly or
fails. Without it pip will satisfy a pinned version from a substituted artefact,
which is the attack the hashes exist to close.

Runs as **uid 10001**, non-root. The application writes nothing to disk — state
is in PostgreSQL — so it needs no writable path of its own. The only tool
installed beyond the runtime is `curl`, for the health check.

The SPA is built **inside** the image from source, so an image cannot ship a
stale `web/dist` somebody forgot to rebuild. `npm run build` runs `tsc && vite
build`, so a type error fails the image build.

## Why Redis, and why it stores nothing durable

Two pieces of state have to agree across replicas.

The **rate limiter** is a security control. Held in process memory it is exact
with one worker and multiplies by N with N of them — three API replicas serve
three times the configured limit. On a serverless host it is worse than
approximate: every invocation may be a new process, so the counter starts empty
on most requests and the limit is not enforced at all.

The **runtime provider override** (`POST /config/llm-provider`) is a live
switch. Process-local, it applied to whichever replica served the POST — so an
operator switching to the deterministic planner would watch the model keep being
used by the other two. The response now says `applies_to: fleet` or
`this_replica_only`, because those are different outcomes.

Neither is persisted, and the compose service runs with `--save "" --appendonly
no` to make that explicit. A rate limit carried across a restart would apply a
caller's old refusals to a fresh process, and the override deliberately does not
survive one.

**Unset `REDIS_URL` and the stack still runs**, per-process. That is a supported
configuration for a single container, not a degraded one — and `/health` says
which is live:

```bash
curl -s localhost:8000/api/health | jq .shared_state
{"backend": "shared", "rate_limit_scope": "all_replicas",
 "provider_override_scope": "all_replicas"}
```

`backend` is `shared`, `process`, or `degraded`. **`degraded` means Redis is
configured and not answering**: the limiter has fallen back to per-process for
those calls rather than failing requests. Failing closed would turn a Redis blip
into an outage of the whole API; failing open would remove the control silently.
The fallback lands on the documented single-process behaviour instead, and says
so.

## Configuration

Everything is an environment variable and nothing is baked in. See
`.env.example` for the full list; the ones that decide behaviour:

| variable | default here | effect |
|---|---|---|
| `LLM_PROVIDER` | `deterministic` | the reasoning path. `/health` reports what was resolved |
| `RAZORPAY_MODE` | `mock` | **the local stack never reaches a real payment provider** |
| `API_TOKEN_SECRET` | *unset* | unset means the development default — see below |
| `MERCHANTOPS_ENV` | *unset* | `production` or `staging` makes the secret mandatory |
| `NOTIFY_CHANNELS` | `log` | naming an unconfigured channel fails at startup |
| `REDIS_URL` | `redis://redis:6379/0` | unset it and state is per-process — see above |

### The token secret

`docker-compose.yml` leaves `API_TOKEN_SECRET` unset, the application falls back
to a development default, and `/health` reports
`auth_secret_is_development_default: true`. That is correct for a laptop and
wrong for anything reachable: the default is a literal in
`app/api/security.py`, so anyone who can read this repository could mint a token
for any user. The permission checks behind the token are real; the identity in
front of them would not be.

For anything beyond localhost, put both in a `.env` beside the compose file:

```bash
API_TOKEN_SECRET=$(openssl rand -hex 32)
MERCHANTOPS_ENV=production
```

`MERCHANTOPS_ENV` is what makes it enforced rather than remembered — with it
set, the application **refuses to start** without a real secret.

## Seeding

The stack starts with an empty schema. `make seed-docker` runs the seeder inside
the API container, so it uses the same `DATABASE_URL` the application does
rather than one typed twice:

```bash
make seed-docker
make token USER_ID=USR_A_OWNER      # a bearer token for the seeded owner
```

## What this is not

**Not a production deployment.** It is the shape of one — separate migrate,
separate worker, non-root, hash-pinned, health-checked — running on one host
with a known database password and no TLS. What it is missing before it is
production is a managed database with backups, a secret store, TLS termination,
an image registry with signed tags, and resource limits. Those are choices about
somebody's infrastructure, not about this application.

**Not a replacement for the Vercel path.** `docs/deploy-vercel.md` still
describes that deployment, and `api/requirements.txt` remains its narrower
dependency set because `@vercel/python` installs from the file beside the
entrypoint. The two share `api/index.py` as the entrypoint, which is why the
container runs `api.index:app` rather than `app.api.main:app`.
