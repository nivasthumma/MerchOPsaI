# ADR 0013 — Closing the residual gaps, and naming the ones that stay open

**Status:** Accepted · 2026-08-25

## Context
The README, threat model and evaluation doc each carried a list of known gaps. Some
were deliberate scope decisions, some were blocked on credentials, and some were
simply unfinished. Presented as one undifferentiated list, a reader cannot tell
"we chose not to" from "we could not" from "we did not get to" — and the third
quietly borrows credibility from the first two.

## Decision
Close every gap that is closeable, and split the remainder by *why* it exists.

### Closed

| Gap | How |
|---|---|
| Audit append-only by convention | PostgreSQL `BEFORE UPDATE/DELETE` triggers on `audit_logs`, plus a server-side `created_at` default so the application cannot backdate an entry. Re-applied inside `reset_schema()` and verified in CI. |
| Header-based authentication | HMAC-SHA256 bearer tokens compared in constant time. Permissions are still read from the database per request, so a token carries identity only. |
| No rate limiting | Per-principal, per-class fixed-window limits. Authentication runs first, so an anonymous flood cannot spend a real user's budget. |
| Verification mutant with no scenario | Scenario UNK-18: the read-back itself fails, and the result must be UNKNOWN. |
| "Only 5 payments externally mapped" | Stale — the expanded dataset maps 19. |
| CI referencing an unimplemented env var | `mutation_test.py` now resolves its interpreter via `MERCHANTOPS_PYTHON` → repo venv → `sys.executable`, and inherits the environment instead of replacing it. |

The audit trigger detail that matters: `drop_all` removes the table *and its
triggers*. Applying the constraint as a one-off manual step would mean the audit
trail becomes silently mutable after any reseed — precisely when nobody is looking.
It is therefore applied inside schema creation, and CI asserts it.

### Left open, deliberately

- Reconciliation is a sweep, not a daemon (§52 excludes Redis/Celery).
- Rate limiting is per-worker, for the same reason.
- Single-process, synchronous.

### Left open, blocked

- Payment execution is mocked — no Razorpay credentials.
- Reasoning is the deterministic planner — no `ANTHROPIC_API_KEY`.
- Replay consistency against a real model — same.

## Not closed on purpose
Two mutants remain caught by unit tests only: idempotency-key derivation and audit
redaction. Both are pure-function properties with no scenario-reachable path. A
fresh key per call is observable only if one approval executes twice, which the
approval state machine prevents; redaction is observable only if something logs a
secret, which nothing does.

Building scenarios for these would raise the scenario count without raising
coverage. That is the same species of dishonesty as inflating a metric, and it is
recorded here rather than quietly done.

---

## Addendum — the "not closed on purpose" section was wrong

The section above argued that two mutants (idempotency-key derivation, audit
redaction) were pure-function properties with no scenario-reachable path, and that
building scenarios for them would be theatre.

Both claims were wrong, and checking instead of reasoning about it is what showed it.

**Audit redaction is reachable.** `runtime.py` records the raw user request on the
`task_created` event, so anything a user pastes into a request passes through
`redact()`. SEC-25 puts a secret in the request and asserts the trail is clean;
with redaction disabled it fails.

**The idempotency `UNIQUE` constraint is reachable**, but only after an attempt that
leaves the refundable balance untouched — in the ordinary path the balance
precondition fires first and the key is never consulted. `ACCEPTED_NOT_APPLIED`
produces that state, and three integration tests now cover the branch, including one
that swaps in a random key to prove the others measure the key and not some other
guard.

**Writing those tests found a real defect.** The duplicate-action handler called
`session.rollback()`, which rolls back the *entire* transaction, not the one failed
INSERT. On collision it would discard the prior action row, the approval decision,
and every audit event written for that task — the safe path destroying the evidence
that it had been taken. Fixed with a SAVEPOINT; added as a 15th mutation.

The general lesson: "this is unreachable, so a test would be contrived" is a claim
about the code, and it needs checking like any other. Here it was load-bearing —
believing it left a transaction-scope bug sitting in the one branch nobody exercised.

---

## Second addendum — the closure claim was also wrong

The addendum above ends by describing SEC-25 and three integration tests as closing
the redaction and idempotency gaps. `docs/evaluation.md` then went further and stated
that every mutant was caught by at least one scenario. Re-running `make mutants` and
reading the output shows it is not:

```
actions: let the caller reuse a spent idempotency key   CAUGHT  0 scenario(s) + unit tests
actions: roll back the whole transaction on a duplicate CAUGHT  0 scenario(s) + unit tests
audit: stop redacting secrets                           CAUGHT  0 scenario(s) + unit tests
```

Two more mutants (registry lookup, argument validation) are detected as a *crash*
rather than a graded failure. Counted strictly, **10 of 15 mutants produce a graded
scenario failure**; all 15 are detected.

SEC-25 is a real scenario — breaking the value-pattern branch of `redact()` fails it
0/1. It simply does not cover the *key-name* branch, which is the half the mutant
disables. That distinction was never checked; the ADR was written from the intent of
the change rather than from the mutation output, which had been printing
`0 scenario(s)` throughout.

One more number in this ADR has since drifted: the "Closed" table says the expanded
dataset maps 19 external payments. `seed_data.py` reports **21** of 589 today. The
README carries the current figures; this table is left as the record of what was
believed then.

The per-mutant accounting now lives in
[`docs/evaluation.md`](../evaluation.md#known-coverage-limits) and is the authority.
This ADR's earlier claims stand as written, corrected here, because how a claim went
wrong is worth more than a clean record.
