"""When to look again, and when to stop — MerchantOps §26, plan P0-15.

The stopping rule the plan asks for is a ladder:

    attempt 1 → retry
    attempt 2 → retry
    attempt 3 → retry
    attempt 4 → retry
    attempt 5 → escalate

That already existed as a comparison — `verify_attempts < max_attempts` in the
sweep's query, and `verify_attempts >= max_attempts` in the operator queue's.
Two call sites, each re-deriving the same rule, and neither writing anything
down. Three things were missing and all three are operator-facing:

**When the next check happens.** The sweep's own cadence was the schedule. An
action re-read three seconds after a provider timeout burns an attempt on a
provider that has not finished failing yet; an action whose cron runs every ten
minutes waits ten minutes whether it is on attempt one or attempt four. The
interval belongs to the action, not to the cron, so it is computed here and
stored on the row. P0-04 requires the UNKNOWN queue to show "next retry", and a
schedule nobody wrote down cannot be shown.

**When the system gave up.** `escalated` was re-derived from a count every time
anyone asked, so an action stuck for six hours and one stuck for six days read
identically, and changing `max_attempts` retroactively un-escalated work a human
had already been handed.

**That giving up is a decision.** `escalate` writes the flag, the timestamp and
an audit event once. Re-running the sweep over an already-escalated action does
not re-escalate it and does not re-notify anyone.

## The backoff

Exponential from a 30-second base, doubling, capped at 15 minutes:

    attempt 1 → +30s     attempt 4 → +4m
    attempt 2 → +1m      attempt 5 → escalate, no further schedule
    attempt 3 → +2m

Capped because unbounded doubling turns attempt 10 into nine hours, and an
action nobody looks at for nine hours is indistinguishable from an action
nobody looks at. Deterministic — no jitter — because this is one sweep against
one provider, not a thundering herd, and a schedule an operator can predict is
worth more here than a spread-out load.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import AgentAction, VerificationState

# The ladder. `MAX_ATTEMPTS` is the number of automatic re-reads an action gets
# before a human owns it; both the sweep and the operator queue take it from
# here rather than each holding a literal 5.
MAX_ATTEMPTS = 5
BASE_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 15 * 60


def backoff_seconds(attempt: int) -> int:
    """How long after attempt `attempt` the next re-read may happen.

    `attempt` is the number of attempts already made, so the first gap is
    `backoff_seconds(1)`. Clamped at both ends: a non-positive attempt count is
    a caller bug rather than a reason to schedule in the past.
    """
    n = max(1, attempt)
    return min(BASE_INTERVAL_SECONDS * (2 ** (n - 1)), MAX_INTERVAL_SECONDS)


def is_settled(state: VerificationState | None) -> bool:
    """A settled action is one nobody needs to look at again."""
    return state in (VerificationState.SUCCESS, VerificationState.FAILED)


def record_attempt(session, action: AgentAction, state: VerificationState | None,
                   *, now: datetime | None = None) -> None:
    """Stamp one verification attempt onto the action.

    Called on every path that re-reads provider state — the sweep, the webhook,
    the operator's Re-verify button, and the initial post-execution check — so
    the schedule cannot be correct on one path and stale on another.

    A settled action gets `next_verify_at = None`: there is nothing to look at
    again, and leaving a schedule on a settled row would put it back in the
    sweep's query the moment somebody widened a filter.
    """
    now = now or datetime.now(UTC)
    action.last_verified_at = now
    if is_settled(state) or action.verify_attempts >= MAX_ATTEMPTS:
        action.next_verify_at = None
    else:
        action.next_verify_at = now + timedelta(
            seconds=backoff_seconds(action.verify_attempts))
    session.flush()


def should_escalate(action: AgentAction) -> bool:
    """Whether this action has exhausted automatic reconciliation.

    Never true for a settled action, however many attempts it took to settle:
    escalation is about unresolved work, and an action that reached SUCCESS on
    attempt five is resolved.
    """
    if action.escalated:
        return False
    if is_settled(action.verification_state):
        return False
    return action.verify_attempts >= MAX_ATTEMPTS


def escalate(session, action: AgentAction, *, reason: str,
             now: datetime | None = None) -> bool:
    """Hand one action to a human. Returns whether this call did it.

    Idempotent: an already-escalated action is left exactly as it was, so a
    sweep that runs every minute does not stamp a new `escalated_at` sixty times
    an hour and bury the moment it actually happened.
    """
    if action.escalated:
        return False

    from app.audit.trace import record
    from app.models import AgentTask

    now = now or datetime.now(UTC)
    action.escalated = True
    action.escalated_at = now
    # An escalated action is out of the automatic loop. Clearing the schedule is
    # what takes it out — the sweep's filter is the schedule, not a second copy
    # of the attempt comparison.
    action.next_verify_at = None
    session.flush()

    task = session.get(AgentTask, action.task_id)
    if task is not None:
        record(session, task, "action_escalated", {
            "action_id": action.id,
            "attempts": action.verify_attempts,
            "verification_state": (action.verification_state.value
                                   if action.verification_state else None),
            "external_reference": action.external_reference,
            "amount_minor": action.amount_minor,
            "reason": reason,
        })
    return True


def due_now(action: AgentAction, *, now: datetime | None = None) -> bool:
    """Whether the sweep may re-read this action yet.

    A NULL schedule means eligible: that is what rows written before this module
    existed carry, and treating them as "never due" would strand exactly the
    backlog the sweep is for.
    """
    if action.next_verify_at is None:
        return True
    now = now or datetime.now(UTC)
    scheduled = action.next_verify_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=UTC)
    return scheduled <= now
