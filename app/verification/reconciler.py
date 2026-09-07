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
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, text
from sqlalchemy import false as sa_false
from sqlalchemy import true as sa_true

from app.audit.trace import record
from app.config import PLATFORM_MARGIN_SECONDS, get_settings
from app.failures import unsettled_states
from app.integrations.razorpay.adapter import get_adapter
from app.models import ActionStatus, AgentAction, AgentTask, TaskStatus, VerificationState
from app.tools.actions import reverify_action
from app.verification.schedule import (
    MAX_ATTEMPTS,
    escalate,
    record_attempt,
    should_escalate,
)

# States that are not yet settled — derived from the failure taxonomy rather
# than listed here (MerchantOps §57). This used to be a literal tuple, and
# app/failures.py separately held an opinion about the same question; they
# disagreed about VERIFICATION_FAILED and neither was in a position to notice.
# One rule, one place, and a mutant that makes an unknown financial state
# retryable now changes what the sweep picks up as well as what the table says.
UNSETTLED = tuple(VerificationState(s) for s in unsettled_states())


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


def abandoned_claim_age_seconds() -> int:
    """How long a claim must sit untouched before the sweep may assume nobody
    is coming back for it.

    Longer than a request can possibly live, and derived from the budget rather
    than guessed, because the thing being waited out is not the provider — it is
    us. A claim is committed before the provider is called, so a row still
    PENDING may belong to a request that is mid-call right now and about to
    write the outcome. Reading its state early and recording FAILED would settle
    an action that then succeeds.
    """
    s = get_settings()
    return s.effective_wall_clock_seconds + PLATFORM_MARGIN_SECONDS


def find_unsettled(session, *, min_age_seconds: int = 30, max_attempts: int = MAX_ATTEMPTS,
                   limit: int = 100, respect_backoff: bool = True) -> list[AgentAction]:
    """Actions that need another look.

    Two populations, with different clocks.

    **Unsettled verifications** — UNKNOWN or PARTIAL. `min_age_seconds` avoids
    racing the *provider*: a refund submitted two seconds ago may simply not
    have propagated, and re-reading it immediately burns an attempt for nothing.

    **Abandoned claims** — PENDING with no verification at all. The request that
    reserved the action died between committing the claim and recording what
    happened, so we hold an idempotency key and no outcome. That is UNKNOWN in
    everything but the column, and it is the state reconciliation exists for: an
    action that may have moved money with nothing on our side that says so.

    Before the claim was made durable this row could not exist — a dying request
    took it down with everything else. Making it survive is only an improvement
    if something then looks at it, which is what this branch is.

    **`respect_backoff`** is the operator's override, and it is a parameter
    rather than something inferred from `min_age_seconds` because the two guard
    different things and a caller must be able to relax them separately.
    `min_age_seconds` waits for the *provider* to propagate; the backoff paces
    *us*. When someone presses "check now" on an action they are looking at,
    they are overriding the pacing deliberately and should not have to also
    claim the provider has propagated. Production sweeps leave it True.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=min_age_seconds)
    abandoned_cutoff = now - timedelta(
        seconds=max(min_age_seconds, abandoned_claim_age_seconds()))
    return (session.query(AgentAction)
            .filter(AgentAction.verify_attempts < max_attempts,
                    # Already handed to a human. The attempt comparison above
                    # would exclude these anyway today, and would stop doing so
                    # the moment somebody raised max_attempts -- pulling work
                    # back out of an operator's queue and re-polling it without
                    # telling them. Escalation is a state, so it is filtered on
                    # as one.
                    AgentAction.escalated.is_(False),
                    # The per-action backoff (P0-15). NULL means never
                    # scheduled, which is what every row written before the
                    # schedule existed carries and what an action that has just
                    # been claimed carries -- both are eligible now.
                    or_(sa_true() if not respect_backoff else sa_false(),
                        AgentAction.next_verify_at.is_(None),
                        AgentAction.next_verify_at <= now),
                    or_(
                        and_(AgentAction.verification_state.in_(list(UNSETTLED)),
                             AgentAction.updated_at <= cutoff),
                        and_(AgentAction.verification_state.is_(None),
                             AgentAction.status == ActionStatus.PENDING,
                             AgentAction.updated_at <= abandoned_cutoff),
                    ))
            # Oldest schedule first, then oldest touch. An action whose next
            # check came due an hour ago should be looked at before one that
            # came due a minute ago, and `limit` makes that ordering matter.
            .order_by(AgentAction.next_verify_at.nullsfirst(),
                      AgentAction.updated_at)
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


def escalate_exhausted(session, *, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Escalate every unsettled action that is out of attempts and has not been
    handed to a human yet. Returns how many this call escalated.

    Escalation used to be re-derived at read time from `verify_attempts >= n`,
    which meant it could not be missed — and could not be recorded either. Now
    it is a stored decision, and a stored decision needs something that
    guarantees it gets made.

    The sweep alone does not. `find_unsettled` filters on
    `verify_attempts < max_attempts`, so an action that reaches the limit by any
    route other than the sweep's own last pass — an operator pressing Re-verify
    five times, a webhook arriving five times, a row already at the limit when
    this code deployed — falls out of the sweep at exactly the moment it stops
    being escalated by anything. It would then appear in neither queue: not in
    the automatic one, because it is out of attempts; not in the human one,
    because nobody set the flag. That is the failure this function exists to
    make impossible.

    So it runs first, on every sweep, over the whole unsettled population rather
    than the page `limit` selects. It is a state-repair pass, and it must not be
    subject to the same pagination as the work it is repairing.
    """
    stuck = (session.query(AgentAction)
             .filter(AgentAction.escalated.is_(False),
                     AgentAction.verify_attempts >= max_attempts,
                     or_(AgentAction.verification_state.in_(list(UNSETTLED)),
                         and_(AgentAction.verification_state.is_(None),
                              AgentAction.status == ActionStatus.PENDING)))
             .all())
    for action in stuck:
        escalate(session, action,
                 reason=f"Out of automatic reconciliation attempts "
                        f"({action.verify_attempts} of {max_attempts}) with the "
                        f"outcome still unestablished.")
    return len(stuck)


def reconcile(session, *, min_age_seconds: int = 30, max_attempts: int = MAX_ATTEMPTS,
              limit: int = 100, respect_backoff: bool = True) -> ReconcileReport:
    report = ReconcileReport()
    adapter = get_adapter(session)

    # Repair first, work second. An action that is already out of attempts is
    # not work this sweep can do; leaving it un-escalated while doing other work
    # is how it stays invisible for another cycle.
    report.escalated += escalate_exhausted(session, max_attempts=max_attempts)

    for action in find_unsettled(session, min_age_seconds=min_age_seconds,
                                 max_attempts=max_attempts, limit=limit,
                                 respect_backoff=respect_backoff):
        report.scanned += 1
        before = action.verification_state
        task = session.get(AgentTask, action.task_id)

        try:
            vr = reverify_action(session, adapter, action)
        except Exception as exc:
            # A failed lookup is not a settlement. Leave the action unsettled
            # and let the next sweep try again -- but on the same ladder as
            # every other attempt. Counting the attempt without scheduling the
            # next one would leave an action whose provider is down eligible on
            # every single pass, spending its five attempts in five seconds and
            # escalating something that a minute's patience would have settled.
            action.verify_attempts += 1
            record_attempt(session, action, action.verification_state)
            report.still_unsettled += 1
            if should_escalate(action):
                escalate(session, action,
                         reason=f"Provider state could not be read after "
                                f"{action.verify_attempts} attempts: "
                                f"{type(exc).__name__}.")
                report.escalated += 1
            report.details.append({
                "action_id": action.id, "task_id": action.task_id,
                "from": before.value if before else None,
                "to": before.value if before else None, "error": str(exc)[:200],
            })
            if task is not None:
                record(session, task, "reconciliation_error",
                       {"action_id": action.id, "error": str(exc)[:200],
                        "attempt": action.verify_attempts})
            continue

        entry = {
            "action_id": action.id,
            # The task this action belongs to. The sweep reports what it read;
            # without this, "ACT_x settled UNKNOWN -> SUCCESS" is a statement an
            # operator cannot follow up on.
            "task_id": action.task_id,
            "from": before.value if before else None,
            "to": vr.state.value,
            "attempt": action.verify_attempts,
            "external_reference": action.external_reference,
        }

        entry["next_verify_at"] = (action.next_verify_at.isoformat()
                                   if action.next_verify_at else None)

        if vr.state in UNSETTLED:
            report.still_unsettled += 1
            if should_escalate(action):
                # Stop sweeping and make it visible to a human. `escalate`
                # writes the flag and the timestamp, so the queue reads a state
                # rather than re-deriving one from the attempt count -- and a
                # second sweep over the same action does not escalate it twice.
                escalate(session, action,
                         reason="Exhausted automatic reconciliation attempts. "
                                "Requires human investigation.")
                report.escalated += 1
                entry["escalated"] = True
                entry["next_verify_at"] = None
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


def unsettled_queue(session, *, merchant_id: str | None = None,
                    escalated_only: bool = False,
                    max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """The reconciliation work queue — plan P0-04.

    UNKNOWN is not a status, it is unresolved financial work, and a queue that
    lists identifiers is not a work queue. Every field the plan names is
    selected here so the UI renders a row rather than assembling one:

        age, amount, provider, external reference, last known state,
        verification attempts, last check, next retry, escalation, owner,
        incident/task

    `escalated_only` is the operator's "what has the system given up on"; the
    default is everything unsettled, including work still in progress. Showing
    only the escalated half leaves an operator blind to the actions about to
    join it.
    """
    params: dict = {"n": max_attempts}
    clauses = ["""(a.verification_state IN ('UNKNOWN', 'PARTIAL')
                   -- An abandoned claim. It keeps a NULL verification_state, so
                   -- listing only the two named states would drop the one
                   -- action nobody has ever established an outcome for.
                   OR (a.verification_state IS NULL AND a.status = 'PENDING'))"""]
    if merchant_id:
        clauses.append("a.merchant_id = :m")
        params["m"] = merchant_id
    if escalated_only:
        clauses.append("a.escalated = true")

    # S608 as above. Every element of `clauses` is a string literal written in
    # this function; the two caller-supplied values (`merchant_id`,
    # `max_attempts`) go into `params` and are bound. Nothing that reaches this
    # f-string came from a request.
    sql = f"""
        SELECT a.id, a.task_id, a.merchant_id, a.action_type,
               a.target_payment_id, a.external_payment_id,
               a.amount_minor, a.external_reference, a.verification_state,
               a.status, a.verify_attempts, a.created_at, a.updated_at,
               a.escalated, a.escalated_at, a.last_verified_at, a.next_verify_at,
               -- Why it is stuck. Without this the queue lists identifiers and
               -- an operator has to open every task to learn what happened.
               a.verification_detail,
               -- Which provider universe this action was placed in. An operator
               -- reconciling by hand needs to know which dashboard to open.
               m.provider, m.environment,
               -- The investigation and incident this came from, so the queue
               -- links back into the workspace rather than dead-ending.
               t.incident_id, t.user_id AS owner
          FROM agent_actions a
          LEFT JOIN agent_tasks t ON t.id = a.task_id
          LEFT JOIN provider_mappings m
            ON m.payment_id = a.target_payment_id AND m.status = 'ACTIVE'
         WHERE {' AND '.join(clauses)}
         -- Escalated first, then oldest. The queue is read top-down under
         -- pressure and the work a human already owns belongs at the top.
         ORDER BY a.escalated DESC, a.created_at
    """  # noqa: S608
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def escalated_actions(session, *, max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """Actions that automatic reconciliation could not settle.

    Kept as the narrow view over `unsettled_queue`. `max_attempts=0` is the
    established way callers ask for "everything still unsettled, not only what
    we gave up on", and that reading is preserved: the escalation flag is the
    filter now, but a caller passing 0 still gets the wider list.
    """
    return unsettled_queue(session, escalated_only=max_attempts > 0,
                           max_attempts=max_attempts)
