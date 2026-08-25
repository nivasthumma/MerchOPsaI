"""Reconciliation sweep tests — closes README limitation #4.

The property under test is not "UNKNOWN gets resolved". It is: unsettled
actions are settled WITHOUT ever re-issuing the financial action, and when they
cannot be settled they become visible rather than being swept forever.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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

    rep = reconcile(db, min_age_seconds=0)

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
    reconcile(db, min_age_seconds=0)
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.failure_code is None
    assert "SUCCESS" in task.final_answer


def test_min_age_guard_skips_fresh_actions(db, owner):
    """A refund submitted seconds ago may simply not have propagated. Burning
    an attempt on it is wasteful and can escalate a healthy action."""
    _unknown_action(db, owner)
    assert find_unsettled(db, min_age_seconds=30) == []
    assert len(find_unsettled(db, min_age_seconds=0)) == 1


def test_settled_actions_are_not_swept_again(db, owner):
    _unknown_action(db, owner)
    first = reconcile(db, min_age_seconds=0)
    assert first.settled == 1
    second = reconcile(db, min_age_seconds=0)
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
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
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
    rep = reconcile(db, min_age_seconds=0)
    assert rep.scanned == 0 and rep.settled == 0
    assert escalated_actions(db) == []


def test_successful_action_is_never_swept(db, owner):
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    assert r["action"].verification_state is VerificationState.SUCCESS
    rep = reconcile(db, min_age_seconds=0)
    assert rep.scanned == 0


def test_escalated_rows_carry_the_reason(db, owner):
    """The operator queue is a work list. Identifiers alone make it a lookup
    exercise: the reason an action is unsettled belongs on the row."""
    from sqlalchemy import text
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.reconciler import escalated_actions

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    assert out.approval is not None
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]
    assert action.verification_state.value == "UNKNOWN"
    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": action.id})

    rows = escalated_actions(db, max_attempts=5)
    row = next(x for x in rows if x["id"] == action.id)
    assert row["verification_detail"], "the queue must say why, not only which"
    assert "reason" in row["verification_detail"]
