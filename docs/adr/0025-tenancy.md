# ADR 0025 — Tenancy, and the second boundary

**Status:** Accepted · 2026-09-01
**Governing spec:** MerchantOps §11, §54

## Context

§11 and §54 both name `tenant_id`. The system had only `merchant_id`, and every isolation
check was written against it.

That is correct for one merchant per tenant and has no way to express two. A business with a
retail and a wholesale entity would need two unrelated logins; a support user covering both
could not exist. Nothing was broken — the gap was expressive, and the kind that gets more
expensive the longer it is left, because every `WHERE merchant_id = :m` would eventually need
a second clause added under time pressure.

## Decision

### 1. Two boundaries, checked outermost first

Tenant isolation does **not** replace merchant isolation. Merchant isolation does the work on
every request; tenant isolation is the check that still holds if merchant isolation is ever
wrong.

They return different rule names — `tenant_isolation` and `merchant_isolation` — so an audit
trail distinguishes them rather than reporting both as "isolation". That is also what makes
them separately testable, which turns out to matter.

### 2. `MERCH_C` exists to be refused

The seed gains a merchant in the *same tenant* as `MERCH_A` that no user is authorised for,
carrying one order and one payment and no traffic.

Without it, every isolation test is also a cross-tenant test. The merchant check could be
deleted entirely and the suite would stay green on the strength of the tenant check alone —
which is precisely the mutant `tenancy: let the tenant check stand in for the merchant check`
injects. `TEN-02` is the scenario that catches it, and it needs a resource to be refused over.

### 3. `tenant_id` is first on `Principal`, and has no default

A default would let a `Principal` be constructed without one, which is exactly the silent
single-tenant assumption the field exists to remove. Every call site says which tenant it is
acting in, and a test asserts the constructor refuses without it.

The tenant is resolved from the database on every request, like permissions and for the same
reason: the token carries identity only.

## Two things this found

### The mutation harness drifted

Rewriting the ownership check moved the code that `policy: stop enforcing merchant isolation`
was anchored to. The mutation silently became a `SKIP` — reported as a survivor, but only
after a full fifty-minute run.

An anchor is a copy of code kept somewhere else, so it drifts when the code moves. **The
harness is subject to the same failure it exists to detect.** `mutation_test.py` now
preflights every anchor before running anything, so drift is a one-second error instead of a
fifty-minute one.

### A survivor that was defence in depth, quietly disabled

Hardcoding a tenant in principal resolution survived the suite. Every API test authenticated
as a user of the same tenant, so a wrong tenant changed nothing observable — and the merchant
check would still have refused the cross-merchant read, so it was not a live hole.

That is the harder kind to notice: not a control that failed, but a redundant control that
stopped being redundant with nothing to say so. Closed by asserting `/me` reports each user's
own tenant.

## Consequences

- `tenants` table; `tenant_id` on `merchants`, `users` and `webhook_events` (§11 names it on
  the event).
- A user still belongs to exactly one merchant. Being in the right tenant is not authority
  over every merchant that tenant owns, and `TEN-02` says so.
- 299 tests, 162 scenarios, 58 mutants.
