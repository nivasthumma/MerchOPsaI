"""Payment links and notifications — MerchantOps §18, §29, §31, §32.

These are the first non-refund actions that reach outside the system. The
property under test is that they inherited every control the refund path has,
rather than acquiring a shortcut because no money moves.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.agent.approval import ApprovalError, approve_and_execute
from app.agent.runtime import AgentRuntime
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import Fault, FaultInjector, ProviderError
from app.models import (
    ActionStatus, AgentAction, Approval, Notification, PaymentLink, TaskStatus,
    VerificationState,
)
from app.tools.recovery_actions import (
    derive_action_key, execute_notification, execute_payment_link,
    verify_notification, verify_payment_link,
)


def _failed_payment(db) -> str:
    return db.execute(text("""
        SELECT id FROM payments
        WHERE merchant_id='MERCH_A' AND status='failed' AND external_payment_id IS NOT NULL
        ORDER BY id LIMIT 1
    """)).scalar() or db.execute(text("""
        SELECT id FROM payments WHERE merchant_id='MERCH_A' AND status='failed'
        ORDER BY id LIMIT 1
    """)).scalar()


def _task_and_approval(db, owner, action_type: str, payload: dict):
    """A task with a pending approval for the given action, without going
    through the planner."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from app.models import AgentTask
    task = AgentTask(id=f"TASK_{uuid.uuid4().hex[:10].upper()}", merchant_id="MERCH_A",
                     user_id=owner.user_id, request="test", status=TaskStatus.AWAITING_APPROVAL,
                     agent_version="t", model_version="t", prompt_version="t")
    db.add(task)
    db.flush()
    ap = Approval(id=f"APR_{uuid.uuid4().hex[:10].upper()}", task_id=task.id,
                  merchant_id="MERCH_A", action_type=action_type, action_payload=payload,
                  evidence=[], risk_level="MEDIUM", decision="PENDING",
                  required_signatures=1,
                  expires_at=datetime.now(timezone.utc) + timedelta(seconds=900))
    db.add(ap)
    db.flush()
    return task, ap


# ------------------------------------------------------------ payment link
def test_a_payment_link_executes_and_is_verified(db, owner):
    pid = _failed_payment(db)
    task, ap = _task_and_approval(db, owner, "generate_payment_link",
                                  {"synthetic_payment_id": pid, "reason": "recovery"})
    r = approve_and_execute(db, task.id, owner)

    action = r["action"]
    assert action.action_type == "payment_link"
    assert action.verification_state is VerificationState.SUCCESS
    assert action.status is ActionStatus.CONFIRMED
    assert action.external_reference.startswith("plink_")
    # Verification re-read the link; it did not trust the create response.
    link = db.get(PaymentLink, action.external_reference)
    assert link is not None and link.status == "created"
    assert "Payment link" in r["task"].final_answer


def test_the_amount_comes_from_the_payment_not_the_request(db, owner):
    """The tool takes no amount. A model-chosen amount is a model-chosen request
    for money."""
    pid = _failed_payment(db)
    expected = db.execute(text("SELECT amount_minor FROM payments WHERE id=:p"),
                          {"p": pid}).scalar()
    task, ap = _task_and_approval(
        db, owner, "generate_payment_link",
        {"synthetic_payment_id": pid, "reason": "r"})
    r = approve_and_execute(db, task.id, owner)
    assert r["action"].amount_minor == expected
    # And the schema gives the model no way to supply one in the first place.
    from app.tools.registry import REGISTRY
    assert "amount_minor" not in REGISTRY["generate_payment_link"].input_schema["properties"]


def test_a_link_is_refused_for_a_payment_that_did_not_fail(db, owner):
    """Re-validated at execution time. A link for a captured payment would ask
    the customer to pay twice."""
    task, ap = _task_and_approval(db, owner, "generate_payment_link",
                                  {"synthetic_payment_id": "SYN_PAY_0002", "reason": "r"})
    r = approve_and_execute(db, task.id, owner)
    assert r["action"] is None
    assert r["result"].data["error"] == "payment_did_not_fail"
    assert db.query(PaymentLink).count() == 0


def test_a_link_is_idempotent_under_the_same_approval(db, owner):
    pid = _failed_payment(db)
    adapter = get_adapter(db)
    task, _ = _task_and_approval(db, owner, "generate_payment_link",
                                 {"synthetic_payment_id": pid, "reason": "r"})
    first = execute_payment_link(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                                 approval_id="APR_FIXED", synthetic_payment_id=pid)
    assert first.result.success, first.result.data
    # The second attempt with the same approval is refused by the UNIQUE key.
    second = execute_payment_link(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                                  approval_id="APR_FIXED", synthetic_payment_id=pid)
    assert second.result.data.get("error") == "duplicate_action"
    assert db.query(PaymentLink).count() == 1


def test_a_provider_timeout_leaves_the_link_unknown_not_failed(db, owner):
    pid = _failed_payment(db)
    task, ap = _task_and_approval(db, owner, "generate_payment_link",
                                  {"synthetic_payment_id": pid, "reason": "r"})
    r = approve_and_execute(db, task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT,
                                                   on_operation="create_payment_link"))
    action = r["action"]
    assert action.verification_state is VerificationState.UNKNOWN
    assert "UNKNOWN" in r["task"].final_answer
    # Never asserted as failed: we do not know whether the customer got a link.
    assert "did not complete" not in r["task"].final_answer


# ----------------------------------------------------------- notification
def test_a_notification_executes_and_is_verified(db, owner):
    task, ap = _task_and_approval(
        db, owner, "send_customer_notification",
        {"customer_id": "SYN_CUS_A0012", "template": "payment_failed_retry",
         "channel": "email", "reason": "recovery"})
    r = approve_and_execute(db, task.id, owner)
    action = r["action"]
    assert action.action_type == "notification"
    assert action.amount_minor == 0
    assert action.verification_state is VerificationState.SUCCESS
    n = db.get(Notification, action.external_reference)
    assert n is not None and n.status == "sent" and n.channel == "email"


def test_an_opted_out_customer_is_never_contacted_even_if_approved(db, owner):
    """§28's opt-out is re-checked at execution, not only in the planner. A human
    approving a stale recommendation must not be able to override it."""
    db.execute(text("UPDATE customers SET contact_opted_out = true WHERE id = 'SYN_CUS_A0012'"))
    db.flush()
    task, ap = _task_and_approval(
        db, owner, "send_customer_notification",
        {"customer_id": "SYN_CUS_A0012", "template": "action_required",
         "channel": "email", "reason": "r"})
    r = approve_and_execute(db, task.id, owner)
    assert r["action"] is None
    assert r["result"].data["error"] == "customer_opted_out"
    assert db.query(Notification).count() == 0


def test_a_notification_cannot_be_sent_twice_under_one_approval(db, owner):
    adapter = get_adapter(db)
    task, _ = _task_and_approval(db, owner, "send_customer_notification", {})
    kw = dict(task_id=task.id, merchant_id="MERCH_A", approval_id="APR_FIXED",
              customer_id="SYN_CUS_A0012", template="action_required", channel="email")
    first = execute_notification(db, adapter, **kw)
    assert first.result.success, first.result.data
    second = execute_notification(db, adapter, **kw)
    assert second.result.data.get("error") == "duplicate_action"
    assert db.query(Notification).count() == 1


def test_an_unreadable_notification_is_unknown_not_sent(db):
    """A provider with no read-back cannot be questioned. Claiming the message
    did not go out would be exactly as unfounded as claiming it did."""
    class NoReadBack:
        def get_notification(self, _):
            return None
    vr = verify_notification(NoReadBack(), notification_id="notif_X")
    assert vr.state is VerificationState.UNKNOWN


def test_a_link_for_the_wrong_amount_is_partial_not_success(db):
    from app.integrations.razorpay.adapter import ExternalPaymentLink

    class WrongAmount:
        def get_payment_link(self, lid):
            return ExternalPaymentLink(id=lid, amount_minor=1, status="created",
                                       short_url="u")
    vr = verify_payment_link(WrongAmount(), link_id="plink_X", expected_amount_minor=5000)
    assert vr.state is VerificationState.PARTIAL


# --------------------------------------------------------------- authority
def test_recovery_actions_need_their_own_permission(db, analyst):
    """`action:recover` is separate from `action:refund`: contacting a customer
    and moving money back to them are different authorities."""
    from app.policy.engine import Decision, PolicyContext, evaluate

    for tool in ("generate_payment_link", "send_customer_notification"):
        pol = evaluate(db, PolicyContext(
            user_id=analyst.user_id, merchant_id="MERCH_A", role=analyst.role,
            permissions=analyst.permissions, tool_name=tool, risk_level="MEDIUM",
            arguments={}))
        assert pol.decision is Decision.DENY
        assert pol.rule == "missing_permission"


def test_medium_risk_actions_still_require_a_human(db, owner):
    from app.policy.engine import Decision, PolicyContext, evaluate

    pid = _failed_payment(db)
    pol = evaluate(db, PolicyContext(
        user_id=owner.user_id, merchant_id="MERCH_A", role=owner.role,
        permissions=owner.permissions, tool_name="generate_payment_link",
        risk_level="MEDIUM", arguments={"synthetic_payment_id": pid, "reason": "r"}))
    assert pol.decision is Decision.REQUIRE_APPROVAL
    assert pol.required_signatures == 1


def test_an_unregistered_action_is_denied_before_the_executor_is_reached(db, owner):
    """Policy re-runs at execution time and refuses a tool that is not in the
    registry, so an approval for one can never reach the provider."""
    task, ap = _task_and_approval(db, owner, "some_future_tool", {})
    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, owner)
    assert e.value.code == "POLICY_DENIED"


def test_a_registered_tool_with_no_executor_is_inert(db, owner, monkeypatch):
    """Defence in depth behind the policy check: a new action tool added to the
    registry but never wired to an executor must be inert, not unguarded."""
    import app.agent.approval as approval_mod

    pid = _failed_payment(db)
    task, ap = _task_and_approval(db, owner, "generate_payment_link",
                                  {"synthetic_payment_id": pid, "reason": "r"})
    monkeypatch.setitem(approval_mod.EXECUTORS, "generate_payment_link", None)
    monkeypatch.delitem(approval_mod.EXECUTORS, "generate_payment_link")

    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, owner)
    assert e.value.code == "TOOL_UNAVAILABLE"
    assert db.query(PaymentLink).count() == 0
