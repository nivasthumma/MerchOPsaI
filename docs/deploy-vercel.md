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
  triggers (`scripts/harden_db.py`), not by application code. A store that
  cannot run them cannot make the guarantee this project claims.
- **Use the pooled connection string**, not the direct one. Every serverless
  instance is a separate process; see "Connection pooling" below.

## Environment variables

Set these in **Project → Settings → Environment Variables**.

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://…` (pooled) | Note the `+psycopg2` driver prefix — a bare `postgres://` URL from a provider's dashboard will not work as-is. |
| `API_TOKEN_SECRET` | a long random string | **Required.** Without it the app falls back to a development default, and tokens minted with that default are forgeable by anyone who can read this repository. `/health` reports the fallback and the UI says so in words. |
| `LLM_PROVIDER` | `deterministic` | Explicit, so the deployment cannot silently become a model deployment if a credential ever appears in the environment. |
| `RAZORPAY_MODE` | `mock` | No outbound financial call. Set to `live_test_mode` with keys only if you mean it. |

Generate a secret with:

    python -c "import secrets; print(secrets.token_urlsafe(48))"

## Prepare the database

Run once, locally, against the hosted database — the function does not
bootstrap its own schema:

    export DATABASE_URL='postgresql+psycopg2://…'   # the DIRECT url, not pooled
    make seed          # creates the schema and the synthetic dataset
    make harden        # applies the audit-log immutability triggers

`make harden` is not optional. `seed` drops the schema, and the triggers go
with it, so a reseed without a re-harden leaves the audit trail quietly
mutable — which is exactly when nobody would notice.

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
