# ADR 0033 — The provider mapping is a table, and escalation is a decision

**Status:** Accepted · 2026-09-07

Two changes from the enterprise production plan (P0-02, P0-04, P0-15). They are one
ADR because they are the same mistake twice: a fact the system depends on was being
*derived* at the moment it was needed rather than *recorded* when it was established.

---

## Context

### The mapping

MerchantOps §6 requires one path from an id this system minted to an id the provider
recognises, and the plan states the failure mode it exists to prevent:

> Never allow a synthetic ID to resolve accidentally to another provider payment.

What implemented it was `payments.external_payment_id`: a nullable `String(64)` with an
ordinary index. It resolved, and it could promise nothing.

- **Nothing enforced uniqueness in either direction.** Two internal payments could
  carry the same `pay_...`. A refund against one would move money against a provider
  object the other also claimed, and the `uq_live_refund_per_payment` index would not
  see it — that index is keyed on the *internal* payment.
- **It recorded no provider.** One column cannot hold a Razorpay id and a Stripe id and
  stay meaningful.
- **It recorded no environment**, which is the dangerous one. A Test Mode id and a live
  id are both short strings beginning `pay_`. Nothing in the column, and nothing in the
  code reading it, distinguished a rehearsal from real money: the process's own
  credentials decided, at call time, which universe an id was interpreted in. Point a
  process with live credentials at a database seeded for Test Mode and every refund
  resolves — against whatever those ids happen to mean in production.

None of these had fired. All three were one configuration change away.

### Escalation

P0-15's stopping rule — five attempts, then escalate — existed as a comparison:
`verify_attempts < max_attempts` in the sweep's query, `verify_attempts >= max_attempts`
in the operator queue's. Two call sites, each re-deriving the same rule, with nothing
written down. Three consequences, all operator-facing:

- **No schedule.** The sweep's cron cadence *was* the backoff. An action re-read three
  seconds after a provider timeout burned an attempt on a provider that had not finished
  failing; an action on a ten-minute cron waited ten minutes whether it was on attempt
  one or attempt four. P0-04 requires the UNKNOWN queue to show "next retry", and a
  schedule nobody wrote down cannot be shown.
- **No moment.** An action stuck for six hours and one stuck for six days read
  identically, because "escalated" was recomputed from a count every time anyone asked.
- **A gap.** An action reaching the limit by any route other than the sweep's own last
  pass — an operator pressing Re-verify five times, five webhook deliveries, a row
  already over the limit when new code deployed — fell out of the sweep (attempts
  exhausted) at exactly the moment nothing marked it escalated. It appeared in neither
  queue. This one was found by a test that had been asserting the old behaviour and
  started failing; it was not hypothetical.

---

## Decision

### `provider_mappings`

A row, with the two constraints that make it a mapping rather than a note:

    uq_mapping_payment_provider_env   one internal payment → at most one external id
                                      per (provider, environment). Resolution is a
                                      function, not a query that might return two
                                      answers.

    uq_mapping_external_identity      one external id → at most one internal payment
                                      per (provider, environment). This is the
                                      direction §6 is actually about.

Both are UNIQUE in PostgreSQL rather than checks in Python, for the reason every other
constraint in this schema is: a check that runs before an INSERT is an optimisation, and
the constraint is the authority.

`app/integrations/mapping.py` is the only resolver. Execution
(`app/tools/actions.py`) and provider reads (`app/tools/verification_tools.py`) both go
through it — a read and a write that resolve an internal id differently is how
"verified the wrong payment" happens. It refuses with a named code
(`not_externally_mapped`, `mapped_to_other_environment`, `merchant_isolation`,
`mapping_retired`, `unknown_payment`) and never with a guess.

**The old columns stay**, and this is deliberate rather than an omission. The mock
adapter answers `get_payment(pay_...)` out of `payments.external_payment_id`, which is
the *provider* holding provider-side state — exactly what Razorpay does with real
credentials. So one fact lives in two places, which is a defect waiting for the day they
diverge. Rather than assume they agree, `check_consistency()` compares them and
`/readiness` reports any drift as `degraded`.

### Escalation as four columns

On `agent_actions`: `escalated`, `escalated_at`, `last_verified_at`, `next_verify_at`.

`app/verification/schedule.py` owns the ladder — 30s doubling to a 15-minute cap, five
attempts, then escalate — and every path that re-reads provider state stamps it:
execution's first verification, the sweep, the webhook, and the operator's Re-verify
button. That is the point of the module existing: the rule was being applied in six
places and got the schedule right in none of them.

`escalate_exhausted()` runs first on every sweep and closes the gap above. It is a
state-repair pass over the whole unsettled population rather than the page `limit`
selects, because it is repairing the thing pagination would hide.

---

## Consequences

**A refund can now be refused for a reason that did not previously exist.** A payment
mapped into `test` while the process runs in `live` reports `mapped_to_other_environment`
rather than resolving. That is the change working.

**`min_age_seconds` and the backoff are separate knobs.** They guard different things —
one waits for the provider to propagate, the other paces us — so `find_unsettled` takes
`respect_backoff` rather than inferring an override from `min_age_seconds=0`. An
operator pressing "check now" overrides the pacing; they should not also have to claim
the provider has propagated.

**The migration is idempotent against a stamped legacy database.**
`scripts/migrate.py` supports a database built by `create_all` and then stamped at the
baseline, which can already hold objects introduced after the revision it is stamped at.
`c3f18a4d7b62` therefore creates only what is absent. Nothing is hidden by this:
`test_head_matches_the_models` compares the final schema against the models and fails on
an object that exists with the wrong shape.

**Two tests changed rather than being made to pass.** `test_escalated_rows_carry_the_reason`
and `test_an_unsettleable_claim_reaches_the_operator_queue` asserted that setting
`verify_attempts = 5` was enough to appear in the operator queue. Under the recorded
model it is not — something has to make the decision — so they now call
`escalate_exhausted` and assert that it does. That is a stronger claim than the one they
made before.

**Not done.** No mapping has ever been confirmed against a real provider:
`verified_at` is null on every row, and null means nobody has checked rather than
checked-and-found. Retiring a mapping is modelled (`MappingStatus.RETIRED`, refused by
`resolve`) but nothing in the application writes it yet; it is reachable only by hand.
