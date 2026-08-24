"""Reconciliation sweep for unsettled actions — closes README limitation #4.

## Why this exists

`UNKNOWN` was resolvable but only if a human clicked Re-verify. An action could
therefore sit unsettled indefinitely, which is exactly the failure mode the
UNKNOWN state was introduced to prevent. Detecting an ambiguous state and then
never resolving it is not safety, it is deferral.

## Why it is a sweep and not a worker

CONTRACT §52 forbids Redis, Celery and Kafka in the MVP, and that call is
correct — a queue would be infrastructure added for its own sake. A sweep is a
plain function over the database:

    scripts/reconcile.py            run it by hand
    */5 * * * * scripts/reconcile.py    or from cron

It is idempotent, bounded, and safe to run concurrently with the application:
every settlement goes through `reverify_action`, which reconciles by
idempotency key and can never issue a second refund.

## What it will not do

- It never retries the *action*. It only re-reads state. A blind retry of a
  financial action whose outcome is unknown is the single most dangerous thing
  this system could do (CONTRACT §35).
- It gives up. After `max_attempts` an action is escalated for human
  investigation rather than swept forever, so a genuinely stuck action becomes
  visible instead of being quietly re-polled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.audit.trace import record
from app.integrations.razorpay.adapter import get_adapter
from app.models import ActionStatus, AgentAction, AgentTask, TaskStatus, VerificationState
from app.tools.actions import reverify_action

# States that are not yet settled. PARTIAL is included: the provider accepted
# the action but the business state does not fully reflect it, which is a
# transient condition worth re-reading.
UNSETTLED = (VerificationState.UNKNOWN, VerificationState.PARTIAL)


@dataclass
class ReconcileReport:
    scanned: int = 0
    settled: int = 0
    still_unsettled: int = 0
    escalated: int = 0
    skipped_too_recent: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned, "settled": self.settled,
            "still_unsettled": self.still_unsettled, "escalated": self.escalated,
            "skipped_too_recent": self.skipped_too_recent, "details": self.details,
        }


def find_unsettled(session, *, min_age_seconds: int = 30, max_attempts: int = 5,
                   limit: int = 100) -> list[AgentAction]:
    """Actions that need another look.

    `min_age_seconds` avoids racing the request that created the action: a
    refund submitted two seconds ago may simply not have propagated yet, and
    re-reading it immediately would burn an attempt for nothing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
    return (session.query(AgentAction)
            .filter(AgentAction.verification_state.in_(list(UNSETTLED)),
                    AgentAction.verify_attempts < max_attempts,
                    AgentAction.updated_at <= cutoff)
            .order_by(AgentAction.updated_at)
            .limit(limit)
            .all())


def _sync_task(session, action: AgentAction, state: VerificationState) -> None:
    """Keep the owning task's user-visible status honest after settlement."""
    task = session.get(AgentTask, action.task_id)
    if task is None:
        return
    if state is VerificationState.SUCCESS:
        task.status = TaskStatus.COMPLETED
        task.failure_code = None
        task.final_answer = (
            f"Reconciliation settled this action: SUCCESS. External reference "
            f"{action.external_reference}.")
    elif state is VerificationState.FAILED:
        task.status = TaskStatus.FAILED
        task.failure_code = "VERIFICATION_FAILED"
        task.final_answer = ("Reconciliation settled this action: the refund did not "
                             "take effect. No money moved.")
    session.flush()


def reconcile(session, *, min_age_seconds: int = 30, max_attempts: int = 5,
              limit: int = 100) -> ReconcileReport:
    report = ReconcileReport()
    adapter = get_adapter(session)

    for action in find_unsettled(session, min_age_seconds=min_age_seconds,
                                 max_attempts=max_attempts, limit=limit):
        report.scanned += 1
        before = action.verification_state
        task = session.get(AgentTask, action.task_id)

        try:
            vr = reverify_action(session, adapter, action)
        except Exception as exc:                                  # noqa: BLE001
            # A failed lookup is not a settlement. Leave the action unsettled
            # and let the next sweep try again.
            action.verify_attempts += 1
            session.flush()
            report.still_unsettled += 1
            report.details.append({
                "action_id": action.id, "from": before.value if before else None,
                "to": before.value if before else None, "error": str(exc)[:200],
            })
            if task is not None:
                record(session, task, "reconciliation_error",
                       {"action_id": action.id, "error": str(exc)[:200],
                        "attempt": action.verify_attempts})
            continue

        entry = {
            "action_id": action.id,
            "from": before.value if before else None,
            "to": vr.state.value,
            "attempt": action.verify_attempts,
            "external_reference": action.external_reference,
        }

        if vr.state in UNSETTLED:
            report.still_unsettled += 1
            if action.verify_attempts >= max_attempts:
                # Stop sweeping and make it visible to a human.
                report.escalated += 1
                entry["escalated"] = True
                if task is not None:
                    record(session, task, "reconciliation_escalated",
                           {"action_id": action.id, "attempts": action.verify_attempts,
                            "state": vr.state.value,
                            "detail": "Exhausted automatic reconciliation attempts. "
                                      "Requires human investigation."})
        else:
            report.settled += 1
            _sync_task(session, action, vr.state)

        if task is not None:
            record(session, task, "reconciliation_attempt", entry)
        report.details.append(entry)

    return report


def escalated_actions(session, *, max_attempts: int = 5) -> list[dict]:
    """Actions that automatic reconciliation could not settle. This is the
    operator's work queue; it must not be silently empty-looking."""
    rows = session.execute(text("""
        SELECT id, task_id, merchant_id, target_payment_id, external_payment_id,
               amount_minor, external_reference, verification_state,
               verify_attempts, updated_at
        FROM agent_actions
        WHERE verification_state IN ('UNKNOWN', 'PARTIAL')
          AND verify_attempts >= :n
        ORDER BY updated_at
    """), {"n": max_attempts}).mappings().all()
    return [dict(r) for r in rows]
