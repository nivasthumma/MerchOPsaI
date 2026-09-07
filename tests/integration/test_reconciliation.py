"""Reconciliation sweep tests — closes README limitation #4.

The property under test is not "UNKNOWN gets resolved". It is: unsettled
actions are settled WITHOUT ever re-issuing the financial action, and when they
cannot be settled they become visible rather than being swept forever.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.models import ActionStatus, AgentAction, Refund, TaskStatus, VerificationState
from app.verification.reconciler import escalated_actions, find_unsettled, reconcile


def _unknown_action(db, owner):
    """Produce a genuinely unsettled action: the refund lands, the response is lost."""
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    assert r["action"].verification_state is VerificationState.UNKNOWN
    return out.task, r["action"]


def test_sweep_settles_unknown_without_reissuing(db, owner):
    task, action = _unknown_action(db, owner)
    before = db.query(Refund).count()

    rep = reconcile(db, min_age_seconds=0, respect_backoff=False)

    assert rep.settled == 1
    assert rep.still_unsettled == 0
    db.refresh(action)
    assert action.verification_state is VerificationState.SUCCESS
    assert action.external_reference is not None
    # The whole point: settlement is a READ, never a retry.
    assert db.query(Refund).count() == before
    n = db.execute(text(
        "SELECT count(*) FROM refunds WHERE payment_id = :p"),
        {"p": action.target_payment_id}).scalar()
    assert n == 1


def test_sweep_updates_the_owning_task(db, owner):
    task, action = _unknown_action(db, owner)
    assert task.failure_code == "EXTERNAL_STATE_UNKNOWN"
    reconcile(db, min_age_seconds=0, respect_backoff=False)
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.failure_code is None
    assert "SUCCESS" in task.final_answer


def test_min_age_guard_skips_fresh_actions(db, owner):
    """A refund submitted seconds ago may simply not have propagated. Burning
    an attempt on it is wasteful and can escalate a healthy action."""
    _unknown_action(db, owner)
    assert find_unsettled(db, min_age_seconds=30) == []
    assert len(find_unsettled(db, min_age_seconds=0, respect_backoff=False)) == 1


def test_settled_actions_are_not_swept_again(db, owner):
    _unknown_action(db, owner)
    first = reconcile(db, min_age_seconds=0, respect_backoff=False)
    assert first.settled == 1
    second = reconcile(db, min_age_seconds=0, respect_backoff=False)
    assert second.scanned == 0, "a settled action was picked up again"


def test_unsettleable_action_escalates_and_stops(db, owner):
    """An action whose external state cannot be read must eventually become a
    human's problem instead of being re-polled indefinitely."""
    task, real = _unknown_action(db, owner)

    stuck = AgentAction(
        id=f"ACT_{uuid.uuid4().hex[:12].upper()}", task_id=task.id,
        merchant_id="MERCH_A", action_type="refund",
        target_payment_id="SYN_PAY_0003",
        external_payment_id="pay_DOES_NOT_EXIST",     # unreadable at the provider
        amount_minor=149900, idempotency_key=f"stuck-{uuid.uuid4().hex}",
        status=ActionStatus.UNKNOWN,
        verification_state=VerificationState.UNKNOWN,
        verify_attempts=4,                            # one below the cap
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.add(stuck)
    db.flush()

    rep = reconcile(db, min_age_seconds=0, max_attempts=5)
    assert rep.escalated == 1

    db.refresh(stuck)
    assert stuck.verification_state is VerificationState.UNKNOWN
    assert stuck.verify_attempts >= 5

    # It is now visible in the operator queue...
    queue = escalated_actions(db, max_attempts=5)
    assert any(a["id"] == stuck.id for a in queue)

    # ...and it is no longer swept.
    again = reconcile(db, min_age_seconds=0, max_attempts=5)
    assert all(d["action_id"] != stuck.id for d in again.details)

    events = [r[0] for r in db.execute(text("""
        SELECT event_type FROM audit_logs WHERE task_id = :t
    """), {"t": task.id}).all()]
    assert "reconciliation_escalated" in events


def test_sweep_is_a_noop_when_nothing_is_unsettled(db, owner):
    AgentRuntime(db, owner).run("Why did revenue drop this week?")
    rep = reconcile(db, min_age_seconds=0, respect_backoff=False)
    assert rep.scanned == 0 and rep.settled == 0
    assert escalated_actions(db) == []


def test_successful_action_is_never_swept(db, owner):
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    assert r["action"].verification_state is VerificationState.SUCCESS
    rep = reconcile(db, min_age_seconds=0, respect_backoff=False)
    assert rep.scanned == 0


def test_escalated_rows_carry_the_reason(db, owner):
    """The operator queue is a work list. Identifiers alone make it a lookup
    exercise: the reason an action is unsettled belongs on the row."""
    from sqlalchemy import text

    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.reconciler import escalate_exhausted, escalated_actions

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    assert out.approval is not None
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]
    assert action.verification_state.value == "UNKNOWN"
    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": action.id})
    db.expire(action)

    # Escalation is a decision the system makes and records, not a comparison a
    # reader re-derives. Spending the attempts is therefore not enough to reach
    # the queue -- something has to notice, and `escalate_exhausted` is what
    # guarantees it notices however the attempts were spent.
    assert escalate_exhausted(db) == 1
    assert action.escalated is True
    assert action.escalated_at is not None

    rows = escalated_actions(db, max_attempts=5)
    row = next(x for x in rows if x["id"] == action.id)
    assert row["verification_detail"], "the queue must say why, not only which"
    assert "reason" in row["verification_detail"]
    # P0-04: the row is a work item, not a lookup key.
    assert row["escalated"] is True
    assert row["amount_minor"] and row["created_at"]
    assert row["verify_attempts"] == 5


def test_escalation_is_recorded_once_however_often_the_sweep_runs(db, owner):
    """`escalated_at` is when the system gave up. A sweep on a one-minute cron
    must not restamp it sixty times an hour and bury that moment."""
    from sqlalchemy import text

    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.reconciler import escalate_exhausted

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]
    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": action.id})
    db.expire(action)

    assert escalate_exhausted(db) == 1
    first = action.escalated_at

    assert escalate_exhausted(db) == 0, "an escalated action must not re-escalate"
    db.refresh(action)
    assert action.escalated_at == first


def test_a_settled_action_never_escalates_however_many_attempts_it_took(db, owner):
    """Escalation is about unresolved work. An action that reached SUCCESS on
    its fifth attempt is resolved, and handing it to a human would be handing
    them a finished job."""
    from sqlalchemy import text

    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.verification.reconciler import escalate_exhausted

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner)
    action = r["action"]
    assert action.verification_state.value == "SUCCESS"
    db.execute(text("UPDATE agent_actions SET verify_attempts = 9 WHERE id = :i"),
               {"i": action.id})
    db.expire(action)

    assert escalate_exhausted(db) == 0
    assert action.escalated is False


def test_a_manual_reverify_that_finally_succeeds_does_not_escalate(db, owner):
    """The gap an 88-mutant run found, and the one call site that needed the
    guard.

    `escalate_exhausted` filters settled actions out in SQL, and both sweep call
    sites are already inside an `if state in UNSETTLED` branch — so the settled
    check inside `should_escalate` is redundant in three of its four callers.
    The fourth is this one, and it is not redundant at all: `reverify` calls
    `should_escalate` unconditionally, *after* deciding what the read found.

    So an operator who presses Re-verify on an UNKNOWN action four times and
    gets a real SUCCESS on the fifth crosses MAX_ATTEMPTS on the attempt that
    resolved it. Without the guard the same action is simultaneously marked
    COMPLETED with "Re-verification resolved the action: SUCCESS" and handed to
    a human with the reason "the outcome is still unestablished". A finished
    refund lands on the escalation queue, and the queue that is supposed to mean
    "somebody must look at this" starts including things nobody needs to look
    at — which is how a queue stops being read.

    Removing the guard survived the whole suite before this test existed.
    """
    from sqlalchemy import text

    from app.agent.approval import approve_and_execute, reverify
    from app.agent.runtime import AgentRuntime
    from app.verification.schedule import MAX_ATTEMPTS

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner)
    action = r["action"]

    # One short of the limit, so the manual attempt below is the one that
    # crosses it. Set directly rather than by pressing the button four times:
    # the point is the state at the boundary, and four real reads would settle
    # it on the first.
    db.execute(text("UPDATE agent_actions SET verify_attempts = :n WHERE id = :i"),
               {"n": MAX_ATTEMPTS - 1, "i": action.id})
    db.expire(action)

    rv = reverify(db, out.task.id, owner)

    # The premises, asserted rather than assumed. If the read did not reach the
    # limit, or did not come back SUCCESS, this test would pass without ever
    # visiting the branch it exists to cover.
    assert rv["verification"].state is VerificationState.SUCCESS
    assert action.verify_attempts >= MAX_ATTEMPTS

    assert action.escalated is False, (
        "a re-verification that RESOLVED the action escalated it — the action "
        "is COMPLETED and on the human queue at the same time")
    assert action.escalated_at is None


def test_the_backoff_widens_and_is_capped():
    """P0-15. Unbounded doubling turns attempt ten into nine hours, and an
    action nobody looks at for nine hours is one nobody looks at."""
    from app.verification.schedule import (
        MAX_INTERVAL_SECONDS,
        backoff_seconds,
    )

    assert backoff_seconds(1) == 30
    assert backoff_seconds(2) == 60
    assert backoff_seconds(3) == 120
    assert backoff_seconds(4) == 240
    # Monotonic up to the cap, then flat -- never shrinking, which would make a
    # struggling provider be polled harder the longer it struggles.
    assert all(backoff_seconds(n) <= backoff_seconds(n + 1) for n in range(1, 20))
    assert backoff_seconds(50) == MAX_INTERVAL_SECONDS


def test_an_unsettled_action_carries_a_next_check_an_operator_can_read(db, owner):
    """P0-04 requires the queue to show "next retry". A schedule nobody wrote
    down cannot be shown."""
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]

    assert action.verification_state.value == "UNKNOWN"
    assert action.last_verified_at is not None, "the last check must be recorded"
    assert action.next_verify_at is not None, "an unsettled action must be scheduled"
    assert action.next_verify_at > action.last_verified_at


def test_a_settled_action_carries_no_schedule(db, owner):
    """Nothing to look at again. Leaving a schedule on a settled row puts it
    back in the sweep the moment somebody widens a filter."""
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    action = approve_and_execute(db, out.task.id, owner)["action"]
    assert action.verification_state.value == "SUCCESS"
    assert action.next_verify_at is None
    assert action.last_verified_at is not None


def test_the_sweep_honours_the_backoff(db, owner):
    """An action whose next check is in the future is not re-read, however
    often the cron fires."""
    from datetime import UTC, datetime, timedelta

    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.reconciler import find_unsettled

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]

    action.next_verify_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    assert action.id not in [a.id for a in find_unsettled(db, min_age_seconds=0)]

    action.next_verify_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    assert action.id in [a.id for a in find_unsettled(db, min_age_seconds=0)]

    # ...and the operator's override reaches it whatever the schedule says.
    action.next_verify_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    assert action.id in [a.id for a in find_unsettled(
        db, min_age_seconds=0, respect_backoff=False)]
