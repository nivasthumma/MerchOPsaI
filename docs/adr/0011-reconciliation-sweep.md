# ADR 0011 — Reconciliation is a sweep, not a worker

**Status:** Accepted · 2026-08-25

## Context
ADR-0005 made `UNKNOWN` resolvable via `POST /tasks/{id}/reverify`. That closed the
mechanism but not the process gap: resolution required a human to notice and click.
An action could therefore sit unsettled indefinitely.

Detecting an ambiguous financial state and then never resolving it is not safety. It
is deferral with extra steps.

## Decision
Add `app/verification/reconciler.py` — a bounded sweep over unsettled actions, run
from `scripts/reconcile.py` on demand or from cron. No queue, no worker, no daemon.

## Rationale
§52 forbids Redis, Celery and Kafka in the MVP, and that call is right: a queue here
would be infrastructure adopted for its own sake. Reconciliation is a query plus a
loop over at most a few rows. A cron line delivers the same outcome with none of the
operational surface.

Three design choices matter more than the mechanism:

1. **The sweep re-reads; it never retries.** Blindly retrying a financial action whose
   outcome is unknown is the most dangerous operation this system could perform
   (§35). Settlement runs through `reverify_action`, which reconciles by idempotency
   key and has no code path that issues a refund.
2. **Min-age guard (30s).** A refund submitted seconds ago may not have propagated.
   Re-reading it immediately burns an attempt and can escalate a healthy action.
3. **Bounded attempts, then escalate.** After 5 tries the action enters an operator
   queue (`GET /actions/escalated`, UI sidebar, CLI exit code 2) instead of being
   re-polled forever. A genuinely stuck action must become visible, not invisible.

## Consequences
- Settlement latency equals the cron interval, not real time. Stated in the README.
- `PARTIAL` is swept alongside `UNKNOWN` — the provider accepted the action but
  business state does not fully reflect it, which is worth re-reading.
- The sweep is idempotent and safe to run concurrently with the application.
- 7 tests cover it, including the escalate-and-stop path and the assertion that the
  refund row count never moves.
