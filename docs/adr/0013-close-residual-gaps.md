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
