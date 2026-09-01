# ADR 0030 — The schema is versioned, and so are the rules that protect it

**Status:** Accepted · 2026-09-01

## Context
There was one way to create the schema:

```python
Base.metadata.drop_all(eng)
Base.metadata.create_all(eng)
```

That is correct for a disposable database and unusable for any other kind. It has no
concept of a change: it builds whatever the models currently say, and the only way to
apply a model change to an existing database is to destroy it first. For a system
whose central claim is an append-only ledger of financial actions, "the schema change
procedure is DROP ALL TABLES" is not a limitation to note in a README — it is the
first thing that makes the claim untrue in practice.

Two consequences followed from it.

**Nothing could detect drift.** `create_all` always produces exactly what the models
declare, so a model change and a schema change were the same event by construction.
There was no artefact to disagree with and therefore no way to be wrong — which
sounds safe and means the opposite once a database exists that was built from an
earlier version of the models.

**The append-only control depended on remembering.** The triggers lived in
`scripts/harden_db.py`. `seed_data.reset_schema()` calls it, so a seeded database was
protected; a database created any other way was not, and nothing reported which kind
you had. `docs/deploy-vercel.md` said "`make harden` is not optional", which is an
accurate description of a control enforced by a person.

## Decision
Alembic, with three things that are not the default setup.

**The URL comes from `app.config`, and `alembic.ini` has no `sqlalchemy.url`.** One
source of truth for the one question where being wrong cannot be undone by running
another migration.

**The audit-immutability triggers are a migration** (`a1c47f9b2e08`), not a script
somebody runs. Schema and the rules protecting it are the same kind of thing and
belong in the same versioned, ordered, applied-once place. `scripts/harden_db.py`
stays as the way to *verify* the control on a database — `verify()` proves the trigger
fires rather than assuming the DDL took effect.

**`scripts/migrate.py`, not `alembic upgrade head`.** A database is in one of three
states and only one of them is a plain upgrade:

```
empty                 -> upgrade from scratch
exists, not stamped   -> stamp the baseline, then upgrade past it
exists, stamped       -> upgrade
```

The middle state is every database that already exists. It has all 23 tables and no
version row, so alembic reads it as empty and fails on `CREATE TABLE audit_logs`.
Detection is by evidence rather than by asking: `audit_logs` present and no version
row means this database predates migrations and is at the baseline by definition.

## Rationale
**Why `reset_schema()` still uses `create_all`.** It is the disposable path — the test
suite and the evaluation runner, which rebuild a known-empty database thousands of
times across a mutation run. Replaying migrations to reach a state the models produce
in one step would buy nothing there.

What makes keeping both paths safe is that they are *proven* identical rather than
assumed to be. `tests/integration/test_migrations.py` upgrades a real database to head
and asserts `compare_metadata()` against `Base.metadata` returns an empty list. Change
a model without writing the migration and that test fails. Autogenerate is normally a
scaffold; here it is the assertion that keeps the fast path honest.

**Why two downgrades refuse.** Alembic generated a baseline downgrade that drops all
23 tables including `audit_logs`, and reversing the immutability migration makes the
audit trail editable. Neither is a migration — one erases the evidence, the other
removes the control over it — and having them one keystroke behind `alembic downgrade`
is a worse risk than being unable to unwind a baseline, which nobody needs to do
anyway. Discarding the schema deliberately still works; it just has to be said
deliberately.

**Why CI runs the migration driver against the seeded database.** `seed_data.py` leaves
CI's database fully populated and unstamped, which is precisely the state every
pre-migration database is in. So the stamp-then-upgrade path is exercised on every run
rather than discovered during a deployment.

## Consequences
- A schema change is now a reviewable artefact. `make migrate-sql` prints the SQL
  without running it, which is the normal path for a production change.
- Migrations must be written expand-then-contract: run before the new code is
  promoted, and leave the old code working. `audit_logs` is append-only by trigger, so
  no migration may rewrite it — adding a nullable column is fine, changing the meaning
  of an existing one is not.
- Every existing database gains the audit-immutability triggers on its first
  `make migrate`, including ones created by a bare `create_all` that never had them.
- Use the **direct** connection URL, not the pooled one. Migrations take locks and a
  pooler can hand them to different backends.
- `alembic` is a new dependency. It is a build/deploy-time tool, not a request-path
  one.
- 5 tests in `tests/integration/test_migrations.py`, including the drift guard and the
  stamp path.
