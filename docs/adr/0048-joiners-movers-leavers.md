# ADR-0048 — Joiners, movers and leavers

**Status:** Accepted · 2026-09-05
**Phase 2 of the readiness review, item three.**

## Context

There was no path from "a customer signs a contract" to "their team can log in"
that did not involve an engineer with database access. Across forty-eight routes
there was no create-user, invite, deactivate, change-role or onboard-merchant.
Principals existed because `scripts/seed_data.py` inserted four of them.

That is a finding an auditor writes up before reading any of the controls it
protects. Joiners-movers-leavers is the process a SOC 2 audit spends most of its
time on, and "an engineer runs an UPDATE" fails it at the first question — not
because the update is wrong, but because nothing records that it happened, who
asked for it, or that the leaver's access actually stopped.

ADR-0047 made roles and permissions rows, which is what made this possible: a
lifecycle API over a JSON column would have been a lifecycle API over a shape
that could not express a revocation.

## Decision

**Within a tenant, its owner administers its people.** `GET/POST /users`,
`PATCH /users/{id}`, `GET/POST /roles`, `PUT /roles/{name}/permissions`. These
are the operations that happen weekly and were costing an engineer each time.

**Creating a tenant is not an API operation.** `scripts/provision.py` creates a
tenant, its first merchant, its roles and its first owner. Exposing that over
HTTP would require a principal standing outside every tenant — an identity with
authority over all customers, reachable with a bearer token. That is a liability
worth more than the convenience, and it is the kind of thing that exists for
eighteen months before somebody notices it has no audit trail of its own.

**Offboarding is a status, never a delete.** `audit_logs.user_id` and
`approval_signatures.user_id` point at the user row; a trail that disappears
when somebody leaves the company is not a trail. `users.status` goes to
DISABLED, with `deactivated_at` and `deactivated_by`.

**`authz.resolve` filters on status by default.** This is the part that makes
offboarding real rather than symbolic. The bearer token never expires and cannot
be revoked (ADR-0025's stated limitation), so the row is the *only* thing that
can stop it — a `resolve` that ignored status would have made "deactivate" a
database change with no effect, and the test that proves otherwise is the first
one in the file.

## The refusals

Creating a user is easy to get right. What is hard is the permanent mistakes,
so they are refused rather than warned about:

**The last active owner cannot be deactivated — or demoted.** Either leaves a
merchant nobody can administer, with no way back that does not involve the
database again. Refusing one and allowing the other would be a door with a lock
on one side, so both are checked by the same function.

**A role somebody holds cannot be deleted.** It would either orphan them or
silently strip their authority, depending on which constraint fired first.

**A permission not in the catalogue is refused.** The catalogue is derived from
the tool registry (ADR-0047), so a typo produces a role that appears to
authorise something and authorises nothing — discovered when somebody is refused
an action they were told they could take.

**A duplicate email is refused with the remedy.** "Re-enable it rather than
creating a second" — because a second account for a leaver who came back is how
one of them gets missed at the next offboarding.

## Consequences

**A created user's token is returned once and stored nowhere.** This is not an
invitation flow, and the response schema says so: authentication is an HMAC of
the user id, so there is no password to set and no acceptance step — creating
the user *is* granting the credential. A real invitation, with an expiring link
the person redeems themselves, arrives with an identity provider and not before.

**Every lifecycle change is audited** — created, role changed, status changed,
permissions changed — and the permission change records what was *granted* and
*revoked* rather than only the new set, because "what changed" is the question
an incident review asks.

**Row-level security bounds all of it independently** (ADR-0046). A user row
inserted with another tenant's id is refused by the policy, not by a code path
that has to remember. The API's own check gives a clean 404 first, so the two
disagree only if one of them is wrong.

**The schema drift guard caught a real difference again.** The migration
declared `status` as `sa.String(16)` where the model has a non-native `Enum` —
which on this dialect is a VARCHAR *plus a CHECK constraint*. A plain String
would have left the column accepting any value the application happened to
write. That guard has now caught something in three consecutive changes.

**The contract check was widened, deliberately.** It required a documented
`200` for every route; creation answers `201`. Requiring 200 would have pushed
these routes to misreport their status code to satisfy a test, which is the
wrong direction for a check that exists to keep the document honest. It now
accepts any 2xx with a schema.

## What this does not do

**Multi-merchant administration.** A tenant may own several merchants, and an
owner administers only their own. Users are merchant-scoped in row-level
security and widening that to the tenant would widen what *every* principal can
read, not just an owner. Administering a sibling merchant remains a provisioning
operation.

**No invitations, no self-service signup, no password reset,** because there are
no passwords. All three arrive with SSO.

**No grant history.** `roles.updated_at` moves and the audit log records the
change, but there is no queryable history of who held what and when — the audit
log is the record, and reconstructing a point-in-time view from it is work
nobody has done. An auditor will eventually ask.

**No bulk operations.** Offboarding forty people at the end of a contract is
forty calls. SCIM is the answer and it is the next item but one.
