# Deploying to Vercel

Vercel serves the SPA as static files and runs the API as a Python function.
The browser only ever talks to its own origin — `/api/...` is a rewrite, not a
cross-origin call, which is the same property the Vite proxy gives you in
development (CONTRACT §41).

## What you need first

A Postgres the function can reach. Neon and Supabase both work; anything that
gives you a pooled connection string does. Two requirements that are easy to
miss:

- **It must be a real Postgres.** The audit log's immutability is enforced by
  triggers (migration `a1c47f9b2e08`), not by application code. A store that
  cannot run them cannot make the guarantee this project claims.
- **Use the pooled connection string**, not the direct one. Every serverless
  instance is a separate process; see "Connection pooling" below.

## Environment variables

Set these in **Project → Settings → Environment Variables**.

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://…` (pooled) | Note the `+psycopg2` driver prefix — a bare `postgres://` URL from a provider's dashboard will not work as-is. |
| `API_TOKEN_SECRET` | a long random string | **Required, and now enforced.** Without it the function fails to start: Vercel sets `VERCEL`, which `require_configured_secret` treats as proof this is not a laptop. Previously the app started anyway and only *reported* the fallback on `/health` — a report nothing consulted. Tokens signed with the default are forgeable by anyone who can read this repository. Set `MERCHANTOPS_ALLOW_DEV_SECRET=1` only for a throwaway preview. |
| `LLM_PROVIDER` | `deterministic` | Explicit, so the deployment cannot silently become a model deployment if a credential ever appears in the environment. |
| `RAZORPAY_MODE` | `mock` | No outbound financial call. Set to `live_test_mode` with keys only if you mean it. |
| `LOG_FORMAT` | `json` | Default. Vercel ingests stdout, so structured logs are searchable with no further setup. |
| `METRICS_SCRAPE_TOKEN` | *(leave unset)* | `/metrics/prometheus` returns 404 without it. Counters reset on every cold start here, so they measure little; the logs are the operational channel on serverless. |

> **Do not set `ANTHROPIC_API_KEY` on this deployment unless you also add
> `anthropic` to `api/requirements.txt`.** Provider selection is `auto` by
> default: it picks `anthropic` the moment it detects a credential, and the
> provider then imports the SDK on first use. With the key set and the package
> absent, every task fails at request time with a `ModuleNotFoundError` that
> looks nothing like a configuration mistake. Setting `LLM_PROVIDER=deterministic`
> as above closes that door; adding the package opens it deliberately.

Generate a secret with:

    python -c "import secrets; print(secrets.token_urlsafe(48))"

## Prepare the database

Run locally against the hosted database — the function does not bootstrap its
own schema. Use the **direct** URL here, not the pooled one; migrations take
locks and a pooler can hand them to different backends.

### First time

    export DATABASE_URL='postgresql+psycopg2://…'   # the DIRECT url, not pooled
    make migrate       # creates the schema and applies every control
    make seed          # loads the synthetic dataset

`make migrate` reports which state it found the database in and, at the end,
proves the audit-log triggers actually reject an UPDATE and a DELETE. Do not
take a clean exit for granted — read those three lines.

### Every deploy after that

    export DATABASE_URL='postgresql+psycopg2://…'
    make migrate-status    # where is it, and what is pending
    make migrate-sql       # the exact SQL, for review
    make migrate           # apply it

Run migrations **before** promoting the new function, and write them so the old
code still works against the new schema — expand first, contract in a later
release. `audit_logs` is append-only by trigger, so no migration may ever
rewrite it; adding a nullable column is fine, changing the meaning of an
existing one is not.

### On a database that predates migrations

It has every table and no `alembic_version` row, so a bare `alembic upgrade
head` reads it as empty and fails trying to create tables that are already
there. `make migrate` detects this and stamps the baseline instead of
re-creating anything. Nothing to do differently — this is noted so the
"stamping" line in its output is not alarming.

### Reseeding

`make seed` drops the schema and rebuilds it from the models, taking the
triggers with it and putting them straight back (`reset_schema` re-applies
them). It is for disposable databases. On anything whose contents matter, use
`make migrate`.

## Deploy

    npx vercel login
    npx vercel --prod

`vercel.json` builds `web/` and routes `/api/*` to `api/index.py`.

## Mint a token to sign in with

    DATABASE_URL='…' API_TOKEN_SECRET='…' python scripts/issue_token.py USR_A_OWNER

The secret must match the one set in Vercel, or the token will not verify.

## Known differences from a long-lived server

These are properties of serverless, not bugs. They are listed because a
control plane that misreports its own guarantees is worse than one that has
fewer of them.

- **Rate limiting is per instance.** `app/api/security.py` keeps its counters
  in a process-local dict, so the documented `5/min` action limit is enforced
  per warm instance rather than globally. The limit still exists; the number is
  no longer exactly what it claims. A shared store (Redis, or a Postgres table)
  is what makes it global again.
- **The reasoning-provider toggle is per instance.** `set_runtime_llm_provider`
  writes to a module global, so a switch made in Settings applies to whichever
  instance served that request and appears to revert on the next one. Set
  `LLM_PROVIDER` as an environment variable instead — that is why the table
  above sets it explicitly.
- **Connection pooling is disabled.** `app/db.py` uses `NullPool` when `VERCEL`
  is set, so an instance holds no connection between invocations. Without it, a
  cold-start spike opens a pool per instance and exhausts the database's
  connection limit while every instance looks healthy.

## Cost of the `/api` prefix

The SPA calls `/api/tasks`; the app defines `/tasks`. In development a Vite
proxy strips the prefix. `api/index.py` does the same at the edge rather than
teaching `app/api/main.py` about its deployment, so the application is byte
identical in both environments.
