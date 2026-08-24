"""Approval and execution service — CONTRACT §21, §23.

The approval button is not the security boundary. Every decision here is
re-checked server-side: the approval record, its expiry, the user's permissions
at execution time, and the payment's preconditions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from app.audit.trace import record
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import FaultInjector
from app.models import (
    AgentAction, AgentTask, Approval, TaskStatus, VerificationState,
)
from app.policy.engine import (
    Decision, PolicyContext, approval_is_valid, evaluate,
)
from app.tools.actions import execute_refund, reverify_action


class ApprovalError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _pending_approval(session, task_id: str) -> Approval | None:
    return session.query(Approval).filter(
        Approval.task_id == task_id, Approval.decision == "PENDING"
    ).order_by(Approval.created_at.desc()).first()


def reject(session, task_id: str, principal, reason: str = "") -> AgentTask:
    task = session.get(AgentTask, task_id)
    ap = _pending_approval(session, task_id)
    if ap is None:
        raise ApprovalError("No pending approval for this task.", "APPROVAL_REJECTED")
    ap.decision = "REJECTED"
    ap.decided_by = principal.user_id
    ap.decided_at = datetime.now(timezone.utc)
    task.status = TaskStatus.REJECTED
    task.failure_code = "APPROVAL_REJECTED"
    task.final_answer = (f"The action was rejected by {principal.user_id}. "
                         f"No external call was made.")
    session.flush()
    record(session, task, "approval_rejected",
           {"approval_id": ap.id, "by": principal.user_id, "reason": reason})
    return task


def approve_and_execute(session, task_id: str, principal,
                        injector: FaultInjector | None = None) -> dict:
    """CONTRACT §21 -> §23. Approval alone does not execute; every gate is
    re-evaluated on the server before the provider is touched."""
    task = session.get(AgentTask, task_id)
    if task is None:
        raise ApprovalError("Unknown task.", "TOOL_INVALID_ARGUMENT")

    ap = _pending_approval(session, task_id)
    if ap is None:
        raise ApprovalError("No pending approval for this task.", "APPROVAL_REJECTED")

    # --- 1. merchant scope of the approver -------------------------------
    if ap.merchant_id != principal.merchant_id:
        record(session, task, "approval_denied",
               {"approval_id": ap.id, "reason": "cross_merchant_approver"})
        raise ApprovalError("Approver belongs to a different merchant.", "AUTHORIZATION_DENIED")

    ap.decision = "APPROVED"
    ap.decided_by = principal.user_id
    ap.decided_at = datetime.now(timezone.utc)
    session.flush()

    # --- 2. approval still valid (expiry) --------------------------------
    ok, why = approval_is_valid(ap)
    if not ok:
        ap.decision = "EXPIRED"
        task.status = TaskStatus.FAILED
        task.failure_code = "APPROVAL_EXPIRED"
        task.final_answer = why
        session.flush()
        record(session, task, "approval_expired", {"approval_id": ap.id, "reason": why})
        raise ApprovalError(why, "APPROVAL_EXPIRED")

    payload = ap.action_payload or {}

    # --- 3. re-run policy at execution time ------------------------------
    # The approver's permissions are re-derived from the session, never taken
    # from the request body.
    ctx = PolicyContext(
        user_id=principal.user_id, merchant_id=principal.merchant_id,
        role=principal.role, permissions=principal.permissions,
        tool_name=ap.action_type, risk_level=ap.risk_level, arguments=payload,
    )
    pol = evaluate(session, ctx)
    record(session, task, "policy_recheck",
           {"approval_id": ap.id, "decision": pol.decision.value, "rule": pol.rule})
    if pol.decision is Decision.DENY:
        task.status = TaskStatus.DENIED
        task.failure_code = "POLICY_DENIED"
        task.final_answer = f"Execution blocked at re-check: {pol.reason}"
        session.flush()
        raise ApprovalError(pol.reason, "POLICY_DENIED")

    # --- 4. execute ------------------------------------------------------
    adapter = get_adapter(session, injector or FaultInjector.disabled())
    record(session, task, "action_executing",
           {"approval_id": ap.id, "adapter_mode": adapter.mode,
            "payment": payload.get("synthetic_payment_id")})

    outcome = execute_refund(
        session, adapter, task_id=task.id, merchant_id=principal.merchant_id,
        synthetic_payment_id=payload["synthetic_payment_id"],
        amount_minor=int(payload["amount_minor"]), approval_id=ap.id,
    )

    result = outcome.result
    action = outcome.action

    if action is not None:
        record(session, task, "action_recorded",
               {"action_id": action.id, "idempotency_key": action.idempotency_key[:16] + "...",
                "external_reference": action.external_reference,
                "status": action.status.value})
        record(session, task, "verification",
               {"action_id": action.id,
                "state": action.verification_state.value if action.verification_state else None,
                "detail": action.verification_detail})

    state = action.verification_state if action else None
    if state is VerificationState.SUCCESS:
        task.status = TaskStatus.COMPLETED
        task.final_answer = (
            f"Refund executed and independently verified. External reference "
            f"{action.external_reference}. Verification: SUCCESS.")
    elif state is VerificationState.UNKNOWN:
        task.status = TaskStatus.COMPLETED
        task.failure_code = "EXTERNAL_STATE_UNKNOWN"
        task.final_answer = (
            "The refund was submitted but its final state could not be determined. "
            "Reported as UNKNOWN. Use re-verify to resolve it; do not assume the "
            "refund did or did not happen.")
    elif state is VerificationState.PARTIAL:
        task.status = TaskStatus.COMPLETED
        task.failure_code = "PARTIAL_EXECUTION"
        task.final_answer = "The refund was accepted but the resulting state is incomplete (PARTIAL)."
    else:
        task.status = TaskStatus.FAILED
        task.failure_code = result.error_code or "VERIFICATION_FAILED"
        task.final_answer = f"The refund did not complete: {result.data}"

    session.flush()
    record(session, task, "task_completed",
           {"status": task.status.value, "verification": state.value if state else None})

    return {"task": task, "action": action, "result": result, "approval": ap,
            "adapter_mode": adapter.mode}


def reverify(session, task_id: str, principal) -> dict:
    """CONTRACT §26 (amended) — the UNKNOWN exit path."""
    task = session.get(AgentTask, task_id)
    if task is None:
        raise ApprovalError("Unknown task.", "TOOL_INVALID_ARGUMENT")
    action = session.query(AgentAction).filter(
        AgentAction.task_id == task_id).order_by(AgentAction.created_at.desc()).first()
    if action is None:
        raise ApprovalError("This task has no external action to verify.",
                            "TOOL_INVALID_ARGUMENT")
    if action.merchant_id != principal.merchant_id:
        raise ApprovalError("Cross-merchant access denied.", "AUTHORIZATION_DENIED")

    adapter = get_adapter(session)
    before = action.verification_state
    vr = reverify_action(session, adapter, action)
    record(session, task, "reverification",
           {"action_id": action.id, "from": before.value if before else None,
            "to": vr.state.value, "attempt": action.verify_attempts,
            "reason": vr.reason})

    if vr.state is VerificationState.SUCCESS:
        task.status = TaskStatus.COMPLETED
        task.failure_code = None
        task.final_answer = (f"Re-verification resolved the action: SUCCESS. "
                             f"External reference {action.external_reference}.")
    elif vr.state is VerificationState.UNKNOWN:
        task.failure_code = "EXTERNAL_STATE_UNKNOWN"
        task.final_answer = ("Re-verification could still not determine the final state. "
                             "Remains UNKNOWN.")
    session.flush()
    return {"action": action, "verification": vr, "task": task}
