"""Dual approval — MerchantOps §25, §26.

The control is not "two clicks". It is **two different people**, and that is
enforced by a UNIQUE constraint rather than by an if-statement — a check that a
retry, a race, or a future refactor could get past.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.agent.approval import ApprovalError, approve_and_execute, reject
from app.agent.runtime import AgentRuntime
from app.models import (
    AgentAction,
    Approval,
    ApprovalSignature,
    Refund,
    TaskStatus,
    VerificationState,
)
from app.policy.engine import Decision, PolicyContext, evaluate

# SYN_PAY_0007 is seeded as partially refunded: 200000 captured, 50000 already
# back, 150000 still refundable. That combination is what makes the uncertainty
# factor reachable at all -- a timed-out FULL refund leaves nothing refundable,
# so the balance check denies the second attempt long before risk is graded. A
# timed-out PARTIAL one leaves both money on the table and an unresolved
# question about what the first attempt did, which is the case worth two pairs
# of eyes.
PARTIAL_PAYMENT = "SYN_PAY_0007"
REMAINING_MINOR = 150000


def _critical_setup(db, owner) -> str:
    """Leave an unsettled action on a payment that still has a balance.

    Written directly rather than driven through the fault injector: the
    injector produces a full-balance refund, and reproducing a partial timeout
    through the planner would be testing the planner. The row is what the risk
    engine reads, and this is that row.
    """
    import uuid as _uuid

    from app.models import ActionStatus, AgentTask

    prior_task = AgentTask(
        id=f"TASK_{_uuid.uuid4().hex[:10].upper()}", merchant_id="MERCH_A",
        user_id=owner.user_id, request="prior attempt whose outcome was lost",
        status=TaskStatus.COMPLETED, agent_version="test", model_version="test",
        prompt_version="test", failure_code="EXTERNAL_STATE_UNKNOWN")
    db.add(prior_task)
    db.flush()

    db.add(AgentAction(
        id=f"ACT_{_uuid.uuid4().hex[:12].upper()}", task_id=prior_task.id,
        merchant_id="MERCH_A", action_type="refund",
        target_payment_id=PARTIAL_PAYMENT,
        external_payment_id=db.execute(text(
            "SELECT external_payment_id FROM payments WHERE id = :p"),
            {"p": PARTIAL_PAYMENT}).scalar(),
        amount_minor=50000,
        idempotency_key=f"prior-unsettled-{_uuid.uuid4().hex}",
        status=ActionStatus.UNKNOWN,
        verification_state=VerificationState.UNKNOWN,
    ))
    db.flush()
    return PARTIAL_PAYMENT


# ------------------------------------------------------------------ policy
def test_critical_risk_demands_two_signatures(db, owner):
    payment = _critical_setup(db, owner)
    pol = evaluate(db, PolicyContext(
        tenant_id=owner.tenant_id, user_id=owner.user_id, merchant_id=owner.merchant_id, role=owner.role,
        permissions=owner.permissions, tool_name="request_refund", risk_level="HIGH",
        arguments={"synthetic_payment_id": payment, "amount_minor": 10000,
                   "reason": "test"}))
    assert pol.decision is Decision.REQUIRE_DUAL_APPROVAL
    assert pol.risk_level == "CRITICAL"
    assert pol.required_signatures == 2
    assert pol.risk.was_raised


def test_ordinary_high_risk_still_needs_only_one(db, owner):
    pol = evaluate(db, PolicyContext(
        tenant_id=owner.tenant_id, user_id=owner.user_id, merchant_id=owner.merchant_id, role=owner.role,
        permissions=owner.permissions, tool_name="request_refund", risk_level="HIGH",
        arguments={"synthetic_payment_id": "SYN_PAY_0002", "amount_minor": 499900,
                   "reason": "duplicate"}))
    assert pol.decision is Decision.REQUIRE_APPROVAL
    assert pol.required_signatures == 1


# --------------------------------------------------------------- signatures
def _pending_dual(db, owner):
    """A task halted on a CRITICAL action awaiting two signatures."""
    payment = _critical_setup(db, owner)
    out = AgentRuntime(db, owner).run(
        f"Refund payment {payment} amount 10000 because the first attempt is unresolved.")
    ap = (db.query(Approval).filter(Approval.task_id == out.task.id,
                                    Approval.decision == "PENDING")
          .order_by(Approval.created_at.desc()).first())
    assert ap is not None, "no approval was raised"
    assert ap.required_signatures == 2
    return out.task, ap


def test_one_signature_does_not_execute(db, owner):
    task, ap = _pending_dual(db, owner)
    refunds_before = db.query(Refund).count()
    actions_before = db.query(AgentAction).count()

    r = approve_and_execute(db, task.id, owner)

    assert r["awaiting_signatures"] == 1
    assert r["action"] is None
    assert db.query(Refund).count() == refunds_before
    assert db.query(AgentAction).count() == actions_before
    db.refresh(ap)
    assert ap.decision == "PENDING"


def test_the_same_person_cannot_sign_twice(db, owner):
    """The whole point. One person clicking approve twice must not satisfy a
    control whose purpose is a second pair of eyes."""
    task, ap = _pending_dual(db, owner)
    approve_and_execute(db, task.id, owner)

    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, owner)
    assert "already signed" in str(e.value).lower()

    assert db.query(ApprovalSignature).filter(
        ApprovalSignature.approval_id == ap.id).count() == 1
    db.refresh(ap)
    assert ap.decision == "PENDING"


def test_a_second_person_completes_it(db, owner, approver):
    task, ap = _pending_dual(db, owner)
    first = approve_and_execute(db, task.id, owner)
    assert first["awaiting_signatures"] == 1

    second = approve_and_execute(db, task.id, approver)
    assert second.get("awaiting_signatures") is None
    assert second["action"] is not None

    db.refresh(ap)
    assert ap.decision == "APPROVED"
    signers = {s.user_id for s in db.query(ApprovalSignature).filter(
        ApprovalSignature.approval_id == ap.id)}
    assert signers == {"USR_A_OWNER", "USR_A_APPROVER"}


def test_the_second_signer_must_still_pass_policy(db, owner, analyst):
    """A second pair of eyes is not a bypass. The analyst has no refund
    permission, so their signature cannot be the one that executes."""
    task, ap = _pending_dual(db, owner)
    approve_and_execute(db, task.id, owner)
    refunds_before = db.query(Refund).count()

    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, analyst)
    assert e.value.code == "POLICY_DENIED"
    # The signature was accepted as a signature and refused as an execution.
    # No money moved on the strength of it.
    assert db.query(Refund).count() == refunds_before


def test_a_cross_merchant_approver_cannot_be_the_second(db, owner, owner_b):
    task, ap = _pending_dual(db, owner)
    approve_and_execute(db, task.id, owner)
    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, owner_b)
    assert e.value.code == "AUTHORIZATION_DENIED"


def test_one_rejection_stops_it(db, owner, approver):
    """Two people to say yes, one to say no. Requiring consensus to stop would
    make the extra approver a weaker control than a single one."""
    task, ap = _pending_dual(db, owner)
    approve_and_execute(db, task.id, owner)
    reject(db, task.id, approver, reason="not warranted")

    db.refresh(ap)
    assert ap.decision == "REJECTED"
    db.refresh(task)
    assert task.status is TaskStatus.REJECTED
    with pytest.raises(ApprovalError):
        approve_and_execute(db, task.id, approver)


def test_the_requirement_is_fixed_at_proposal_time(db, owner):
    """A later policy change must not quietly reduce what an in-flight action
    needs, so the count lives on the approval record."""
    task, ap = _pending_dual(db, owner)
    db.execute(text("UPDATE agent_actions SET verification_state = 'SUCCESS' "
                    "WHERE verification_state = 'UNKNOWN'"))
    db.flush()
    # The condition that made it CRITICAL is gone; the requirement is not.
    r = approve_and_execute(db, task.id, owner)
    assert r["awaiting_signatures"] == 1
