# ADR-0047 — Roles and permissions are rows

**Status:** Accepted · 2026-09-05
**Phase 2 of the readiness review, item two. Closes the §66 gap the coverage audit recorded.**

## Context

Permissions were a JSON list on `users`, and `role` was a string beside it. The
spec-coverage audit recorded this honestly: *"permissions are a JSON list on
`users`. Works; not a table, so not queryable or auditable as a set."*

Four things follow from that, and only the first is obvious.

- **"Who can approve a CRITICAL refund?" is not a query.** It is a full scan and
  a JSON parse per row, written fresh each time somebody asks.
- **A permission cannot be revoked from a role.** It can only be removed from
  each user who happens to hold it, one row at a time, and the one that gets
  missed is invisible.
- **A tenant cannot define its own role.** Every deployment shares one hard-coded
  vocabulary, so an enterprise wanting `treasury-approver` needs a code change.
- **There is no access review.** SOC 2 asks quarterly who holds what; producing
  it meant a `psql` session, which is why it was never produced.

## Decision

Three tables. A `permissions` catalogue, tenant-owned `roles`, and the
`role_permissions` join. `users.role` and `users.permissions` are gone;
`users.role_id` points at a role.

**One role per user.** That is what this system has always modelled — `role` was
a single string — and `principal.role` is read in a dozen places where "the
role" has to keep meaning something. Several roles per user is a real enterprise
need and is deliberately not this change.

**Roles are tenant-owned, with no global built-ins.** A customer defining a role
should not be editing a definition shared with every other customer.
`ensure_default_roles` gives a new tenant the standard three, and it is part of
creating a tenant rather than a step somebody remembers.

**The catalogue is derived, not maintained.** `authz.catalogue()` is built from
`app.tools.registry` — every permission a registered tool declares — plus the two
reads no single tool owns. A hand-written second list beside the one the policy
engine gates on is how a tool comes to require `action:refunds` while the
catalogue offers `action:refund`, with nobody finding out until somebody tries.
A test asserts every permission the registry requires exists as a row.

**One resolver.** `authz.resolve` is the only answer to "what may this user do",
and both callers that need it — the API's `current_principal` and the worker
claiming a queued task — go through it. Authority read in two places is
authority that will eventually be read two ways.

## The backfill refuses rather than guesses

Every existing `(tenant_id, role)` pair becomes a role; its permissions are the
union of what its users held. Union rather than intersection, because losing a
permission somebody currently has is a silent privilege *removal*.

But if two users share a role name within a tenant and hold *different*
permissions, the union grants both the larger set — a silent privilege
*addition*, which is the worse failure. **The migration raises and names the
conflicting pairs.** It is a question about real people's access, and a schema
change is the wrong place to answer it quietly.

Exercised in both directions on a database built by migrations alone: three
legacy users backfilled to two roles with their authority unchanged, the
downgrade restored the JSON exactly, and making two `owner` users disagree made
the upgrade refuse with both variants named.

## Consequences

**`GET /access-review`** returns the tenant's roles, its users, and what each
holds, with an `as_of`. Owner only: a list of who can move money is exactly the
reconnaissance a read-only token would want.

**Revocation now means something.** Deleting one `role_permissions` row removes
the permission from everybody holding that role, which is what a revocation is.
The tests that used to edit a JSON column now delete that row, and they are
better tests for it.

**Two duplications were found by breaking them.**

`tests/conftest.py` had its own copy of the seeder's insert loop. The day roles
became rows the copy fell behind, inserted users with no role, and 443 tests
errored on a NOT NULL constraint. Both paths now call `seeder.insert_all`.

The `users.role_id` foreign key was unnamed, so `create_all` invented
`users_role_id_fkey` while the migration created `fk_users_role` — and the
downgrade failed to drop a constraint that did not exist under that name on a
seeded database. Named in the model, so both paths agree. The schema drift guard
does not compare constraint names, which is worth knowing.

**Row-level security covers the new tables** (ADR-0046): `roles` by tenant,
`role_permissions` through its parent. `permissions` is deliberately uncovered —
a catalogue of names every tenant draws from, with no tenant to scope it to — and
is listed in `UNSCOPED_TABLES`, because silence is not a decision.

## What this does not do

**No role management API.** Roles can be read and are provisioned by
`ensure_default_roles`; creating, editing and assigning them still means a
database write. That is the next item — tenant and user lifecycle — and it is
the one that finally takes engineers out of the customer-provisioning path.

**No multi-role users, no role hierarchy, no scoped grants.** A permission is
held or not; there is no "may refund up to ₹10,000", because amount limits are
policy and live in `app/policy/engine.py` where risk is computed.

**Nothing is versioned yet.** `roles.updated_at` moves when a role changes, but
there is no history of who changed a grant and when. The audit log records the
actions permissions gate, not the grants themselves — which is a gap an auditor
will eventually ask about.
