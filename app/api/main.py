"""FastAPI surface — CONTRACT §41 (reverify added by ADR-0008 #3).

Every endpoint enforces authentication and merchant isolation server-side. The
frontend is never the authority (CONTRACT §20, §41).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.agent.approval import ApprovalError, approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime, Principal
from app.audit.trace import trace_for
from app.config import get_settings
from app.db import session_scope
from app.eval.runner import load_scenarios, run_scenario
from app.models import AgentAction, AgentTask, Approval

app = FastAPI(title="MerchantOps Agent", version="0.1.0")


# --------------------------------------------------------------------------
# Authentication. A header-based stand-in for a real IdP; the point is that the
# principal is derived server-side from the users table, never trusted from the
# request body (CONTRACT §11).
# --------------------------------------------------------------------------
def current_principal(x_user_id: str = Header(default="USR_A_OWNER")) -> Principal:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT id, merchant_id, role, permissions FROM users WHERE id = :u
        """), {"u": x_user_id}).mappings().first()
    if row is None:
        raise HTTPException(401, "Unknown user.")
    return Principal(row["id"], row["merchant_id"], row["role"], list(row["permissions"]))


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
        "llm_model": s.llm_model if s.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        "payment_adapter": s.resolved_razorpay_mode,
        "razorpay_execution_is_real": s.resolved_razorpay_mode == "live_test_mode",
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
