# ADR-0046 — The second wall

**Status:** Accepted · 2026-09-05
**Phase 2 of the readiness review, item one.**

## Context

This session opened by finding, in the working tree, an interrupted
mutation-test run that had left

```python
if False:  # MUTANT
    return PolicyResult(Decision.DENY, "… belongs to another merchant …")
```

in `app/policy/engine.py`. The application started. Seven hundred tests passed.
Merchant A could act on merchant B's orders, and nothing anywhere said so.

The isolation model was not wrong. Every route resolves its principal
server-side, every query carries `WHERE merchant_id = :m`, and the tenant
boundary is checked outermost-first. It was one wall. One `if`, restated in
forty-eight places, each of which has to be right every time.

## Decision

PostgreSQL row-level security, bound to the authenticated principal.

**The binding is automatic.** `current_principal` puts the principal in a
context variable; `session_scope` pushes it onto the transaction with
`SET LOCAL`. No route changed. A control that forty-eight routes must each
remember is one that most of them eventually will not.

**`SET LOCAL`, never `SET`.** On a pooled connection a session-lifetime setting
outlives the request that made it and applies to whichever request gets that
connection next — one merchant's scope serving another's traffic, which is worse
than no scope at all.

**`FORCE ROW LEVEL SECURITY`, not merely `ENABLE`.** The application role owns
these tables, and PostgreSQL exempts a table's owner from its own policies
unless forced. Enabling alone produces the worst outcome available: a control
that reports as present, passes an inspection of `pg_policies`, and filters
nothing.

**`FOR ALL … WITH CHECK`, not just `USING`.** The boundary applies to writes.
Without it the read boundary could be walked around by inserting a row into
another merchant and reading it back.

**Child tables inherit rather than restate.** `tool_calls`, `agent_messages`,
`approval_signatures` and `incident_evidence` carry no merchant of their own and
are filtered by an `EXISTS` against their parent — which is itself under RLS, so
the subquery sees only in-scope rows.

**Two tables are deliberately uncovered.** `evaluation_results` is a scenario
run and `worker_heartbeats` is a process saying it is alive: platform data with
no merchant to scope to, and inventing one would invent a relationship that does
not exist. They are named in `UNSCOPED_TABLES`, because silence is not a
decision, and a test asserts every table is either covered or listed.

## What this does not claim

**An unbound session is unrestricted, not blocked.** With no principal bound,
`app.merchant_id` is empty and every policy passes.

Fail-closed is the stronger control, and it would require every sweep, script,
migration and seeder to declare itself — roughly thirty call sites, with the
thirty-first silently failing closed at some later date. So the wall stands
where the risk is: the authenticated request path. Background code is the
trusted plane, reviewed as such. Making it fail closed is a worthwhile second
step and is not this one.

This is stated in the module, in the migration, and in a test that asserts an
unbound session still sees every merchant — so the limit cannot be quietly
forgotten or later mistaken for a capability boundary.

## Consequences

**A latent framework trap was found by the test that mattered.** The first
version bound the scope in `current_principal` while it was a *sync* dependency.
FastAPI runs a sync dependency and a sync endpoint in two different threadpool
contexts, each a copy of the request's — so the ContextVar set in the dependency
was invisible to the endpoint, and the binding silently did nothing. Every
existing test passed, because the application's own checks were still in place.
It was found only by the test that removes those checks and asserts the request
fails anyway. `current_principal` is now `async`, with the blocking work in
`run_in_threadpool` where it already was, so the binding happens in the
request's own task context.

That is the second time in this session that a control appeared to work and did
not, and both times the thing that caught it was a test written to fail if the
control were absent rather than to pass if it were present.

**A create_all database had no policies at all.** `seed_data.reset_schema`
builds the schema from `Base.metadata`, and a policy is not in `Base.metadata` —
so the migration ran, reported success, and `pg_policies` was empty. The audit
triggers had already solved this: the DDL lives in `scripts/harden_db.py` and is
applied after `create_all`. Row-level security now does the same, with the
migration keeping its own frozen copy (a migration that imports live code stops
being a snapshot) and a test asserting the two agree.

**`migrate.py` and `make harden` now verify the boundary filters,** rather than
that the DDL ran — the same distinction `verify()` already drew for the audit
trigger. It binds a merchant and counts what a supposedly-scoped session can
still see.

**The worker binds per task and sheds it after.** It is the one process that
runs work for several merchants in sequence, which makes it where a leaked
binding would matter most — and where the sweeps that follow must still see
everything.

## Verified

- A `SELECT` with no `WHERE` at all returns one merchant's rows when bound, and
  every merchant's when not.
- Naming another merchant explicitly returns zero rows.
- An `INSERT` into another merchant is refused by the policy.
- `_owned` with its merchant check deleted — the mutant, at the route — returns
  404 instead of another merchant's task, and still returns 200 to the owner.
- 26 policies, all forced.
