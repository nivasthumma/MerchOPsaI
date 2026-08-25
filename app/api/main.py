"""FastAPI surface — CONTRACT §41 (reverify added by ADR-0008 #3).

Every endpoint enforces authentication and merchant isolation server-side. The
frontend is never the authority (CONTRACT §20, §41).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.agent.approval import ApprovalError, approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime, Principal
from app.api.security import DEV_SECRET_IN_USE, current_principal
from app.audit.trace import trace_for
from app.config import get_settings
from app.db import session_scope
from app.eval.runner import load_scenarios, run_scenario
from app.verification.reconciler import escalated_actions, reconcile
from app.models import AgentAction, AgentTask, Approval, ToolCall

app = FastAPI(title="MerchantOps Agent", version="0.1.0")


# Authentication and rate limiting live in app/api/security.py. The caller
# presents a signed bearer token it cannot forge; permissions are still read
# from the database on every request, never carried in the token (CONTRACT §11).


class TaskRequest(BaseModel):
    request: str


def _task_view(s, task: AgentTask) -> dict:
    approvals = s.query(Approval).filter(Approval.task_id == task.id).all()
    actions = s.query(AgentAction).filter(AgentAction.task_id == task.id).all()
    return {
        "id": task.id, "merchant_id": task.merchant_id, "user_id": task.user_id,
        "request": task.request, "status": task.status.value,
        "final_answer": task.final_answer, "failure_code": task.failure_code,
        "findings": task.findings, "tool_calls": task.tool_call_count,
        "llm_turns": task.llm_turn_count, "duration_ms": task.duration_ms,
        "agent_version": task.agent_version, "model_version": task.model_version,
        "prompt_version": task.prompt_version,
        "is_replay": task.is_replay, "replayed_from": task.replayed_from,
        "approvals": [{
            "id": a.id, "decision": a.decision, "action_type": a.action_type,
            "action_payload": a.action_payload, "risk_level": a.risk_level,
            "expires_at": a.expires_at.isoformat(), "decided_by": a.decided_by,
        } for a in approvals],
        "actions": [{
            "id": a.id, "action_type": a.action_type, "status": a.status.value,
            "target_payment_id": a.target_payment_id,
            "external_payment_id": a.external_payment_id,
            "amount_minor": a.amount_minor, "external_reference": a.external_reference,
            "verification_state": a.verification_state.value if a.verification_state else None,
            "verification_detail": a.verification_detail,
            "verify_attempts": a.verify_attempts,
        } for a in actions],
    }


def _owned(s, task_id: str, principal: Principal) -> AgentTask:
    task = s.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "Unknown task.")
    if task.merchant_id != principal.merchant_id:
        # Do not leak existence across merchants.
        raise HTTPException(404, "Unknown task.")
    return task


@app.get("/health")
def health():
    s = get_settings()
    return {
        "status": "ok",
        "llm_provider": s.resolved_llm_provider,
        # The source, not the credential. Without it a `deterministic` reading
        # is ambiguous: deliberately configured, or no credentials found?
        "llm_credential_source": s.anthropic_credential_source,
        "llm_provider_is_explicit": s.llm_provider != "auto",
        "llm_model": s.llm_model if s.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        "payment_adapter": s.resolved_razorpay_mode,
        "razorpay_execution_is_real": s.resolved_razorpay_mode == "live_test_mode",
        "auth": "bearer_hmac",
        "auth_secret_is_development_default": DEV_SECRET_IN_USE,
    }


@app.post("/tasks")
def create_task(body: TaskRequest, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        out = AgentRuntime(s, principal).run(body.request)
        return _task_view(s, out.task)


@app.get("/tasks/{task_id}")
def get_task(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        return _task_view(s, _owned(s, task_id, principal))


@app.get("/tasks/{task_id}/trace")
def get_trace(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        return {"task_id": task_id, "trace": trace_for(s, task_id)}


@app.get("/tasks/{task_id}/evidence")
def get_evidence(task_id: str, principal: Principal = Depends(current_principal)):
    """The evidence behind a task's findings and its pending action.

    CONTRACT §21 says the human reviews payment, amount, reason, **evidence**
    and risk before approving. The Streamlit UI reads `tool_calls` straight from
    the database to do that; an HTTP client had no route to the same facts, so a
    non-Streamlit approval screen could only show four of the five.

    `untrusted` is carried through deliberately (CONTRACT §36). Merchant and
    customer free text is an injection surface, and a client needs to know which
    values to render as quarantined data rather than as system text. Stripping
    the flag here would push that judgement onto every consumer.
    """
    with session_scope() as s:
        _owned(s, task_id, principal)
        rows = s.query(ToolCall).filter(ToolCall.task_id == task_id) \
            .order_by(ToolCall.seq).all()
        return {
            "task_id": task_id,
            "tool_calls": [{
                "id": c.id, "seq": c.seq, "tool": c.tool_name,
                "arguments": c.input, "success": c.success,
                "error_code": c.error_code, "risk_level": c.risk_level,
                "policy_decision": c.policy_decision, "duration_ms": c.duration_ms,
                "evidence": (c.output or {}).get("evidence", []),
                "data": (c.output or {}).get("data", {}),
            } for c in rows],
        }


@app.post("/tasks/{task_id}/approve")
def approve(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            r = approve_and_execute(s, task_id, principal)
        except ApprovalError as e:
            raise HTTPException(409, {"error": str(e), "code": e.code})
        return _task_view(s, r["task"])


@app.post("/tasks/{task_id}/reject")
def reject_task(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            task = reject(s, task_id, principal)
        except ApprovalError as e:
            raise HTTPException(409, {"error": str(e), "code": e.code})
        return _task_view(s, task)


@app.post("/tasks/{task_id}/reverify")
def reverify_task(task_id: str, principal: Principal = Depends(current_principal)):
    """CONTRACT §26 (amended) — the UNKNOWN exit path."""
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            r = reverify(s, task_id, principal)
        except ApprovalError as e:
            raise HTTPException(409, {"error": str(e), "code": e.code})
        return {"task": _task_view(s, r["task"]),
                "verification": r["verification"].as_dict()}


@app.post("/tasks/{task_id}/replay")
def replay_task(task_id: str, mode: str = "PLAYBACK",
                principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        if mode.upper() == "RE_REASON":
            return re_reason(s, task_id, principal)
        return playback(s, task_id)


@app.post("/actions/reconcile")
def run_reconcile(min_age_seconds: int = 30, max_attempts: int = 5,
                  principal: Principal = Depends(current_principal)):
    """Settle actions left UNKNOWN/PARTIAL. Re-reads state only; never retries
    the financial action."""
    with session_scope() as s:
        rep = reconcile(s, min_age_seconds=min_age_seconds, max_attempts=max_attempts)
        return rep.as_dict()


@app.get("/actions/escalated")
def list_escalated(max_attempts: int = 5,
                   principal: Principal = Depends(current_principal)):
    """Actions automatic reconciliation could not settle — the operator queue."""
    with session_scope() as s:
        rows = escalated_actions(s, max_attempts=max_attempts)
    # Merchant isolation applies here too.
    return [r for r in rows if r["merchant_id"] == principal.merchant_id]


@app.get("/scenarios")
def list_scenarios():
    return [{"id": s.id, "category": s.category, "critical": s.critical,
             "description": s.description} for s in load_scenarios()]


@app.post("/scenarios/{scenario_id}/run")
def run_one(scenario_id: str):
    scen = {s.id: s for s in load_scenarios()}
    if scenario_id not in scen:
        raise HTTPException(404, "Unknown scenario.")
    import uuid
    with session_scope() as s:
        res = run_scenario(s, scen[scenario_id], f"RUN_{uuid.uuid4().hex[:8].upper()}")
        return {"scenario_id": res.scenario_id, "passed": res.passed,
                "checks": res.checks, "metrics": res.metrics}
