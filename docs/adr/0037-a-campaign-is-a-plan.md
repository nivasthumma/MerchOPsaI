# ADR 0037 — A recovery campaign is a recovery plan, and the fifth bound belongs to it

**Status:** Accepted · 2026-09-02

## Context

MerchantOps v2 §37 introduces the recovery campaign:

```text
RC-017
Objective:                 Recover failed payments
Affected:                  1,842
Eligible:                  1,126
Expected recovery:         ₹3.4L
Budget:                    ₹4L
Maximum attempts/customer: 2
Status:                    ACTIVE
```

and §38 gives it five explicit bounds: maximum financial amount, maximum
actions, maximum attempts per customer, maximum duration, and maximum risk.

Read as a backlog item this looks like a new subsystem. Read against
`app/models.py` it mostly is not. `RecoveryPlan` already carries the objective
(`intervention`), the §22 computed figures, a status, `plan_key` unique per
incident so a second planning pass cannot open a parallel campaign with its own
separate budget, and **four of §38's five bounds** — all four enforced in
`app/recovery/stopping.py`. Its candidates are the affected transactions.

`RecoveryPlan` *is* v2's campaign, under v1's name.

## Decision

**No `campaigns` table.** Two records of one thing means two places a budget can
be enforced and one of them eventually forgotten. v2 §103 makes this argument
itself: enterprise grade "does not mean more microservices, more infrastructure,
more dashboards" — it means "clear ownership, explicit state, controlled
authority".

`app/recovery/campaign.py` is a **projection**: §37's card computed from the
plan and its candidates at read time. Two things were genuinely missing, and
neither was an entity.

### 1. §38's fifth bound was not a property of the campaign

Maximum risk was enforced — as `MAX_UNATTENDED_RISK`, a module constant shared
by every campaign. That is a real control and it is not what §38 asks for. Its
sentence is "Every campaign must have explicit limits", and a limit that lives
only in the source is not explicit: an approver reading a plan could not tell
what risk it was authorised to take.

`recovery_plans.max_risk_level` is copied at creation like the other four, for
the reason the plan's docstring already gave about budgets: the bounds are part
of the decision that authorised the campaign. Lowering the global ceiling must
not silently retighten a campaign already in flight, and raising it must not
silently widen one.

### 2. Budget consumption was computed and thrown away

`check_budget` worked out spend-so-far internally, once per decision, and
discarded it. §37 shows a budget beside an expected recovery, but the number a
merchant watching an ACTIVE campaign actually wants is how much of it is gone.
Each bound is now reported beside its consumption.

**An attempt counts against the budget even when it fails.** A failed attempt
still spent an action and still reached a customer; a budget that counted only
successes would let a campaign retry indefinitely at no recorded cost, which is
the opposite of a bound.

## `exhausted` reports and does not decide

The card names bounds already used up. It does not act on them.
`evaluate_stopping_rules` remains the sole authority on whether a campaign may
continue — a second decider is a second place for the two to disagree, and the
one a merchant is looking at is the worse of the two to be wrong.

## Consequences

- The migration adds a `NOT NULL` column to a populated table, so it back-fills
  `'HIGH'` — exactly what the constant enforced — and drops the default
  immediately. An in-flight plan keeps the bound it was already running under.
  (Autogenerate emitted it with no `server_default` for the third time; that
  failure mode is now a habit worth watching for.)
- Every card figure is derived rather than cached. A cached count disagrees with
  its rows the first time a candidate moves, and these are numbers read while a
  campaign is running.
- `GET /campaigns` excludes finished campaigns; `GET /campaigns/{id}` does not,
  because "what happened to RC-017" is asked most often about one that ended.
- §37's `RC-017` identifier is not adopted. Plans have ids; adding a second
  human-facing sequence would mean two names for one row and a question about
  which one an audit record cites. If the console wants a short label it can
  render one from the id.
