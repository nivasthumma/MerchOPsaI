"""Approval and execution service — CONTRACT §21, §23.

The approval button is not the security boundary. Every decision here is
re-checked server-side: the approval record, its expiry, the user's permissions
at execution time, and the payment's preconditions.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.agent.provenance import policy_input_hash
from app.audit.trace import record
from app.incidents.lifecycle import advance as move_incident
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import FaultInjector
from app.models import (
    AgentAction,
    AgentTask,
    Approval,
    ApprovalSignature,
    TaskStatus,
    VerificationState,
)
from app.models import IncidentStatus as _S
from app.policy.engine import (
    POLICY_VERSION,
    Decision,
    PolicyContext,
    approval_is_valid,
    evaluate,
)
from app.tools.actions import execute_refund, reverify_action
from app.tools.recovery_actions import execute_notification, execute_payment_link
from app.verification.schedule import escalate, should_escalate


def _run_refund(session, adapter, *, task_id, merchant_id, approval_id, **payload):
    return execute_refund(session, adapter, task_id=task_id, merchant_id=merchant_id,
                          synthetic_payment_id=payload["synthetic_payment_id"],
                          amount_minor=int(payload["amount_minor"]),
                          approval_id=approval_id)


# The only operations that may reach the provider, and the only way they may do
# it. A tool absent from this map cannot execute even if policy approved it --
# fail-closed, so a new action tool that forgets to register here is inert
# rather than unguarded.
EXECUTORS = {
    "request_refund": _run_refund,
    "generate_payment_link": execute_payment_link,
    "send_customer_notification": execute_notification,
}

# What a successful execution is called, per action type. Refund-specific prose
# on a notification would tell an operator money moved when none did.
_SUCCESS_PROSE = {
    "request_refund": "Refund executed and independently verified.",
    "generate_payment_link": "Payment link created and independently verified.",
    "send_customer_notification": "Notification sent and independently verified.",
}


class ApprovalError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _pending_approval(session, task_id: str) -> Approval | None:
    """The pending approval, LOCKED for the rest of this transaction.

    Approve, reject and revoke all decide from this row, and without the lock
    they could each read PENDING and each act: an approver flushes APPROVED
    and calls the provider after its checkpoint, while a concurrent revoke that
    read PENDING overwrites the decision -- the record says REVOKED, "no
    external call was made", and a refund went out. With `FOR UPDATE` the
    second caller waits, re-reads the row once the first commits, finds it no
    longer PENDING, and is told there is nothing to decide.
    """
    return session.query(Approval).filter(
        Approval.task_id == task_id, Approval.decision == "PENDING"
    ).order_by(Approval.created_at.desc()).with_for_update().first()


def _sign(session, approval: Approval, user_id: str, decision: str) -> bool:
    """Record one person's decision. False if they had already signed.

    The UNIQUE(approval_id, user_id) constraint is the authority here, not a
    prior SELECT. MerchantOps §26 requires the second approver to be a
    *different* person, and a check-then-insert is a race that two clicks from
    one user can win. Attempting the write and letting the constraint refuse it
    is the only version that holds under concurrency.
    """
    sp = session.begin_nested()
    try:
        session.add(ApprovalSignature(
            id=f"SIG_{uuid.uuid4().hex[:10].upper()}", approval_id=approval.id,
            user_id=user_id, decision=decision))
        session.flush()
        sp.commit()
        return True
    except IntegrityError:
        sp.rollback()
        return False


def _approved_signatures(session, approval_id: str) -> list[ApprovalSignature]:
    return (session.query(ApprovalSignature)
            .filter(ApprovalSignature.approval_id == approval_id,
                    ApprovalSignature.decision == "APPROVED")
            .order_by(ApprovalSignature.signed_at).all())


def reject(session, task_id: str, principal, reason: str = "") -> AgentTask:
    task = session.get(AgentTask, task_id)
    ap = _pending_approval(session, task_id)
    if ap is None:
        raise ApprovalError("No pending approval for this task.", "APPROVAL_REJECTED")
    # One veto is enough. A dual-approval action needs two people to say yes
    # and only one to say no -- requiring consensus to *stop* would make the
    # extra approver a weaker control than a single one.
    _sign(session, ap, principal.user_id, "REJECTED")
    ap.decision = "REJECTED"
    ap.decided_by = principal.user_id
    ap.decided_at = datetime.now(UTC)
    task.status = TaskStatus.REJECTED
    task.failure_code = "APPROVAL_REJECTED"
    task.final_answer = (f"The action was rejected by {principal.user_id}. "
                         f"No external call was made.")
    session.flush()
    record(session, task, "approval_rejected",
           {"approval_id": ap.id, "by": principal.user_id, "reason": reason})
    return task


def revoke(session, task_id: str, principal, reason: str = "") -> AgentTask:
    """Withdraw a pending approval that has begun collecting signatures.

    An approval that needs two people can sit with one signature on it. The
    person who signed can take it back, and so can anyone who could have
    rejected it. Afterwards there is no pending approval, so nothing can
    complete it: `approve_and_execute` finds none, and `approval_is_valid`
    refuses any decision but APPROVED.
    """
    task = session.get(AgentTask, task_id)
    ap = _pending_approval(session, task_id)
    if task is None or ap is None:
        raise ApprovalError("No pending approval for this task.", "APPROVAL_REJECTED")
    if ap.merchant_id != principal.merchant_id:
        raise ApprovalError("Approver belongs to a different merchant.", "AUTHORIZATION_DENIED")
    # Withdrawing an authorisation is itself an authorisation decision: only
    # someone who could have approved this action may take one back.
    from app.tools.registry import REGISTRY
    spec = REGISTRY.get(ap.action_type)
    needed = set(spec.required_permissions) if spec else set()
    if not needed <= set(principal.permissions):
        record(session, task, "approval_denied",
               {"approval_id": ap.id, "reason": "revoke_without_permission",
                "by": principal.user_id})
        raise ApprovalError(
            f"Revoking this approval needs {', '.join(sorted(needed - set(principal.permissions)))}.",
            "AUTHORIZATION_DENIED")
    ap.decision = "REVOKED"
    ap.decided_by = principal.user_id
    ap.decided_at = datetime.now(UTC)
    task.status = TaskStatus.REJECTED
    task.failure_code = "APPROVAL_REJECTED"
    task.final_answer = (f"Approval {ap.id} was revoked by {principal.user_id} before it "
                         f"could execute. No external call was made.")
    session.flush()
    record(session, task, "approval_revoked",
           {"approval_id": ap.id, "by": principal.user_id, "reason": reason,
            "signatures_withdrawn": [s.user_id for s in _approved_signatures(session, ap.id)]})
    return task


def approve_and_execute(session, task_id: str, principal,
                        injector: FaultInjector | None = None) -> dict:
    """CONTRACT §21 -> §23. Approval alone does not execute; every gate is
    re-evaluated on the server before the provider is touched."""
    task = session.get(AgentTask, task_id)
    if task is None:
        raise ApprovalError("Unknown task.", "TOOL_INVALID_ARGUMENT")

    # --- 0. a replay never executes ----------------------------------------
    # A RE_REASON replay halts at the approval gate like any run, so it leaves
    # an approval behind. Approving THAT would re-issue a historical financial
    # action against the live provider -- the one side effect replay must never
    # have (CONTRACT §28). Refused here, at the only door to execution.
    if task.is_replay:
        record(session, task, "approval_denied", {"reason": "replay_task"})
        raise ApprovalError("This task is a replay. A replay never executes an "
                            "action; approve the original task instead.",
                            "AUTHORIZATION_DENIED")

    ap = _pending_approval(session, task_id)
    if ap is None:
        raise ApprovalError("No pending approval for this task.", "APPROVAL_REJECTED")

    # --- 1. merchant scope of the approver -------------------------------
    if ap.merchant_id != principal.merchant_id:
        record(session, task, "approval_denied",
               {"approval_id": ap.id, "reason": "cross_merchant_approver"})
        raise ApprovalError("Approver belongs to a different merchant.", "AUTHORIZATION_DENIED")

    # --- 1b. record this person's signature (MerchantOps §26) ------------
    if not _sign(session, ap, principal.user_id, "APPROVED"):
        record(session, task, "approval_denied",
               {"approval_id": ap.id, "reason": "duplicate_signature",
                "by": principal.user_id})
        raise ApprovalError(
            f"{principal.user_id} has already signed approval {ap.id}. "
            f"A second signature must come from a different person.",
            "APPROVAL_REJECTED")

    signatures = _approved_signatures(session, ap.id)
    if len(signatures) < ap.required_signatures:
        # Not yet executable. The task stays where it is, and nothing external
        # has been touched.
        remaining = ap.required_signatures - len(signatures)
        record(session, task, "approval_signed", {
            "approval_id": ap.id, "by": principal.user_id,
            "signatures": len(signatures), "required": ap.required_signatures})
        task.final_answer = (
            f"Approval {ap.id} signed by {principal.user_id}. "
            f"{remaining} further approval(s) required from a different person "
            f"before this action can execute. No external call was made.")
        session.flush()
        return {"task": task, "action": None, "result": None, "approval": ap,
                "adapter_mode": None,
                "awaiting_signatures": remaining,
                "signatures": [s.user_id for s in signatures]}

    ap.decision = "APPROVED"
    ap.decided_by = principal.user_id
    ap.decided_at = datetime.now(UTC)
    session.flush()
    record(session, task, "approval_granted", {
        "approval_id": ap.id, "signatures": [s.user_id for s in signatures],
        "required": ap.required_signatures})
    # v2 §20. The gap between a granted approval and the act. Previously
    # invisible: an approval granted against a provider that was down looked
    # identical to one nobody had answered.
    move_incident(session, task, _S.APPROVED, reason="Approval granted.")

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

    # --- 2b. the approved request is the request being executed ----------
    # The snapshot pins what the human saw. A payload that no longer hashes to
    # it is a different request, and nobody approved that one.
    if ap.policy_input_hash is not None and ap.policy_input_hash != policy_input_hash(
            action_type=ap.action_type, arguments=payload,
            merchant_id=ap.merchant_id, risk_level=ap.risk_level):
        task.status = TaskStatus.DENIED
        task.failure_code = "POLICY_DENIED"
        task.final_answer = ("Execution refused: the action no longer matches what "
                             "was approved. No external call was made.")
        session.flush()
        record(session, task, "approval_denied",
               {"approval_id": ap.id, "reason": "payload_changed_since_approval"})
        raise ApprovalError(task.final_answer, "POLICY_DENIED")

    # --- 3. re-run policy at execution time ------------------------------
    # The approver's permissions are re-derived from the session, never taken
    # from the request body.
    ctx = PolicyContext(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id, merchant_id=principal.merchant_id,
        role=principal.role, permissions=principal.permissions,
        tool_name=ap.action_type, risk_level=ap.risk_level, arguments=payload,
    )
    pol = evaluate(session, ctx)
    # Both policies on the record: the one the human approved under, and the
    # one execution is permitted by. They differ after a policy change, and the
    # trail should show that rather than leave it to be inferred.
    record(session, task, "policy_recheck",
           {"approval_id": ap.id, "decision": pol.decision.value, "rule": pol.rule,
            "duration_ms": pol.duration_ms,
            "approved_under_policy": ap.policy_version,
            "approved_decision": ap.policy_decision,
            "executing_under_policy": POLICY_VERSION})
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
    move_incident(session, task, _S.EXECUTING, reason="Provider action started.")

    executor = EXECUTORS.get(ap.action_type)
    if executor is None:
        # Registered, approved, and still not executable. Better than the
        # alternative: a tool reaching the provider through a path nobody
        # reviewed because it was never wired up deliberately.
        task.status = TaskStatus.FAILED
        task.failure_code = "TOOL_UNAVAILABLE"
        task.final_answer = (f"'{ap.action_type}' has no registered executor; "
                             f"no external call was made.")
        session.flush()
        raise ApprovalError(task.final_answer, "TOOL_UNAVAILABLE")

    outcome = executor(session, adapter, task_id=task.id,
                       merchant_id=principal.merchant_id, approval_id=ap.id,
                       **payload)

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
        move_incident(session, task, _S.VERIFYING,
                      reason="Verifying external state.")

    state = action.verification_state if action else None
    what = _SUCCESS_PROSE.get(ap.action_type, "The action completed and was verified.")
    noun = {"request_refund": "refund", "generate_payment_link": "payment link",
            "send_customer_notification": "notification"}.get(ap.action_type, "action")
    if state is VerificationState.SUCCESS:
        task.status = TaskStatus.COMPLETED
        task.final_answer = (f"{what} External reference {action.external_reference}. "
                             f"Verification: SUCCESS.")
    elif state is VerificationState.UNKNOWN:
        task.status = TaskStatus.COMPLETED
        task.failure_code = "EXTERNAL_STATE_UNKNOWN"
        task.final_answer = (
            f"The {noun} was submitted but its final state could not be determined. "
            f"Reported as UNKNOWN. Use re-verify to resolve it; do not assume the "
            f"{noun} did or did not happen.")
    elif state is VerificationState.PARTIAL:
        task.status = TaskStatus.COMPLETED
        task.failure_code = "PARTIAL_EXECUTION"
        task.final_answer = (f"The {noun} was accepted but the resulting state is "
                             f"incomplete (PARTIAL).")
    else:
        task.status = TaskStatus.FAILED
        task.failure_code = result.error_code or "VERIFICATION_FAILED"
        task.final_answer = f"The {noun} did not complete: {result.data}"

    # v2 §20. Where the incident goes next follows the verification outcome,
    # and the two branches are deliberately different states:
    #
    #   SUCCESS  -> MEASURING    the act settled; the ledger totals what it was
    #                            worth (§49). RESOLVED used to claim both "we
    #                            stopped acting" and "we know what it came to".
    #   UNKNOWN  -> RECONCILING  somebody is looking. Distinct from the UNKNOWN
    #                            state, which says nobody could tell (§53).
    #
    # PARTIAL and failure are left where they are: a partial execution is not
    # settled and not unresolvable, and moving it to either would assert
    # something nobody established.
    if state is VerificationState.SUCCESS:
        move_incident(session, task, _S.MEASURING,
                      reason="Action verified; measuring the outcome.")
    elif state is VerificationState.UNKNOWN:
        move_incident(session, task, _S.RECONCILING,
                      reason="External state undetermined; handed to reconciliation.")

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

    # The manual path spends the same attempts as the sweep does, so it has to
    # honour the same stopping rule (P0-15). Without this, an operator pressing
    # Re-verify five times would push the action past the limit by hand: out of
    # the sweep's reach, still un-escalated, and therefore in neither queue.
    if should_escalate(action):
        escalate(session, action,
                 reason=f"Out of automatic reconciliation attempts "
                        f"({action.verify_attempts}); the last was a manual "
                        f"re-verification by {principal.user_id}.")

    session.flush()
    return {"action": action, "verification": vr, "task": task}
