"""FastAPI surface — CONTRACT §41 (reverify added by ADR-0008 #3).

Every endpoint enforces authentication and merchant isolation server-side. The
frontend is never the authority (CONTRACT §20, §41).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from types import SimpleNamespace

from datetime import timedelta

from sqlalchemy import case, func, select, text

from app.agent.approval import ApprovalError, approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime, Principal
from app.api.security import DEV_SECRET_IN_USE, current_principal
from app.audit.trace import record, trace_for, trace_for_incident
from app.config import get_settings, set_runtime_llm_provider
from app.db import session_scope
from app.detection import detect
from app.detection.engine import open_incidents
from app.incidents.lifecycle import legal_from
from app.incidents.manager import investigate
from app.eval.runner import load_scenarios, run_scenario
from app.verification.reconciler import escalated_actions, reconcile
from app.models import (
    AgentAction, AgentTask, Approval, Incident, IncidentEvidence, ToolCall,
    VerificationState, utcnow,
)

app = FastAPI(title="MerchantOps Agent", version="0.1.0")


# Authentication and rate limiting live in app/api/security.py. The caller
# presents a signed bearer token it cannot forge; permissions are still read
# from the database on every request, never carried in the token (CONTRACT §11).


class TaskRequest(BaseModel):
    request: str


class ProviderRequest(BaseModel):
    provider: str


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
        # `runtime` means someone switched it in this process; it does not
        # survive a restart, and a published metric was not measured under it
        # unless the report says so.
        "llm_provider_source": s.llm_provider_source,
        "llm_model": s.llm_model if s.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        "payment_adapter": s.resolved_razorpay_mode,
        "razorpay_execution_is_real": s.resolved_razorpay_mode == "live_test_mode",
        "auth": "bearer_hmac",
        "auth_secret_is_development_default": DEV_SECRET_IN_USE,
    }


@app.get("/me")
def whoami(principal: Principal = Depends(current_principal)):
    """Who the caller is, as the server understands it.

    The client never asserts this. It is returned so an interface can show the
    acting identity — the same screen behaves differently for an owner and an
    analyst, and an operator should not have to infer which one they are.
    """
    return {"user_id": principal.user_id, "merchant_id": principal.merchant_id,
            "role": principal.role, "permissions": principal.permissions}


@app.post("/config/llm-provider")
def set_llm_provider(body: ProviderRequest,
                     principal: Principal = Depends(current_principal)):
    """Switch between providers that are already configured.

    Deliberately narrow. It selects among providers the process can already
    reach; it never accepts a credential. CONTRACT §37 keeps secrets in the
    environment, and a browser form is not that.

    The override is process-local and does not survive a restart — with more
    than one worker, each would hold its own. Persisting it would make the
    active provider a piece of durable state that no environment variable
    explains, which is a worse trade.
    """
    s = get_settings()
    choice = body.provider.lower()
    if choice not in {"auto", "deterministic", "anthropic"}:
        raise HTTPException(422, {"error": f"Unknown provider '{body.provider}'.",
                                  "code": "unknown_provider"})
    if principal.role != "owner":
        # 403 rather than 404: this is not a merchant-scoped resource, so
        # refusing plainly leaks nothing.
        raise HTTPException(403, {"error": "Changing the provider requires the owner role.",
                                  "code": "role_required"})
    if choice == "anthropic" and not s.anthropic_credential_source:
        raise HTTPException(409, {
            "error": "No Anthropic credential is configured on the server. "
                     "Set ANTHROPIC_API_KEY or sign in with `ant auth login`, then retry.",
            "code": "no_credential"})

    before = s.resolved_llm_provider
    set_runtime_llm_provider(None if choice == "auto" else choice)
    after = s.resolved_llm_provider

    with session_scope() as sess:
        # Audited like any other privileged change. No task owns this event,
        # which is why audit_logs.task_id is nullable.
        record(sess, SimpleNamespace(id=None, merchant_id=principal.merchant_id,
                                     user_id=principal.user_id),
               "llm_provider_changed",
               {"from": before, "to": after, "requested": choice,
                "by": principal.user_id, "role": principal.role})

    return {"llm_provider": after, "llm_provider_source": s.llm_provider_source,
            "llm_model": s.llm_model if after == "anthropic" else "deterministic-planner-v1",
            "changed_from": before}


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


# --------------------------------------------------------------------------
# Incidents — MerchantOps §13, §65
# --------------------------------------------------------------------------
def _incident_view(s, inc: Incident, *, detail: bool = False) -> dict:
    view = {
        "id": inc.id, "merchant_id": inc.merchant_id,
        "type": inc.incident_type.value, "severity": inc.severity.value,
        "status": inc.status.value, "title": inc.title, "summary": inc.summary,
        "revenue_at_risk_minor": inc.revenue_at_risk_minor,
        "detection_rule": inc.detection_rule,
        "detection_version": inc.detection_version,
        "correlation_id": inc.correlation_id,
        "started_at": inc.started_at.isoformat(),
        "detected_at": inc.detected_at.isoformat(),
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
    }
    if not detail:
        return view

    view["signals"] = inc.signals
    view["evidence"] = [{
        "id": e.id, "key": e.key, "value": e.value.get("v"),
        "source": e.source, "untrusted": e.untrusted,
    } for e in s.query(IncidentEvidence)
        .filter(IncidentEvidence.incident_id == inc.id)
        .order_by(IncidentEvidence.id).all()]
    view["tasks"] = [{
        "id": t.id, "status": t.status.value, "final_answer": t.final_answer,
        "tool_calls": t.tool_call_count, "duration_ms": t.duration_ms,
    } for t in s.query(AgentTask)
        .filter(AgentTask.incident_id == inc.id)
        .order_by(AgentTask.created_at).all()]
    # Which moves are available from here. The UI renders this; it never
    # computes it, for the same reason it never computes a policy outcome.
    view["legal_transitions"] = sorted(x.value for x in legal_from(inc.status))
    return view


def _owned_incident(s, incident_id: str, principal: Principal) -> Incident:
    inc = s.get(Incident, incident_id)
    if inc is None or inc.merchant_id != principal.merchant_id:
        # No distinction between absent and forbidden (MerchantOps §54).
        raise HTTPException(404, "Unknown incident.")
    return inc


@app.get("/incidents")
def list_incidents(include_closed: bool = False,
                   principal: Principal = Depends(current_principal)):
    """The operations console. Scoped to the caller's merchant, ordered by
    revenue at risk — the largest problem is the one to open first."""
    with session_scope() as s:
        if include_closed:
            rows = (s.query(Incident)
                    .filter(Incident.merchant_id == principal.merchant_id)
                    .order_by(Incident.revenue_at_risk_minor.desc(),
                              Incident.detected_at.desc()).all())
        else:
            rows = open_incidents(s, principal.merchant_id)
        return {"incidents": [_incident_view(s, i) for i in rows],
                "total_revenue_at_risk_minor": sum(i.revenue_at_risk_minor for i in rows)}


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        return _incident_view(s, _owned_incident(s, incident_id, principal), detail=True)


@app.get("/incidents/{incident_id}/trace")
def get_incident_trace(incident_id: str,
                       principal: Principal = Depends(current_principal)):
    """MerchantOps §58 — detection, every lifecycle move, and every event of
    every task the incident dispatched, in one ordering."""
    with session_scope() as s:
        _owned_incident(s, incident_id, principal)
        return {"incident_id": incident_id, "trace": trace_for_incident(s, incident_id)}


@app.post("/incidents/detect")
def run_detection(principal: Principal = Depends(current_principal)):
    """Run the detection sweep for the caller's merchant.

    Idempotent: re-running over the same window records `already_known` rather
    than creating a second incident for one anomaly.
    """
    with session_scope() as s:
        return detect(s, principal.merchant_id).as_dict()


@app.post("/incidents/{incident_id}/investigate")
def investigate_incident(incident_id: str,
                         principal: Principal = Depends(current_principal)):
    """Dispatch the bounded agent against an incident.

    The incident's next state is derived from the task's recorded status, never
    from the model's prose (MerchantOps §38).
    """
    with session_scope() as s:
        inc = _owned_incident(s, incident_id, principal)
        try:
            r = investigate(s, inc, principal)
        except ValueError as e:
            raise HTTPException(409, {"error": str(e), "code": "INCIDENT_NOT_OPEN"})
        except PermissionError:
            raise HTTPException(404, "Unknown incident.")
        return {"incident": _incident_view(s, r["incident"], detail=True),
                "task": _task_view(s, r["task"])}


@app.get("/metrics")
def metrics(window_hours: int = 24,
            principal: Principal = Depends(current_principal)):
    """Counts for the operations strip, scoped to the caller's merchant.

    Everything here is derived from rows this merchant owns. There is no
    cross-merchant aggregate and there will not be one on this route: the
    isolation that applies to a task applies to a count of tasks.

    `moved_minor` deliberately counts only actions that were both executed and
    independently verified as SUCCESS. An action that is UNKNOWN has not been
    shown to have moved money, and this number must never imply it did — the
    unsettled queue is what reports those, and it reports them as unsettled.
    """
    since = utcnow() - timedelta(hours=window_hours)
    m = principal.merchant_id

    with session_scope() as s:
        gated = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "PENDING")) or 0

        approved = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "APPROVED",
                   Approval.decided_at >= since)) or 0

        rejected = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "REJECTED",
                   Approval.decided_at >= since)) or 0

        moved_minor = s.scalar(
            select(func.coalesce(func.sum(AgentAction.amount_minor), 0))
            .where(AgentAction.merchant_id == m,
                   AgentAction.verification_state == VerificationState.SUCCESS,
                   AgentAction.created_at >= since)) or 0

        calls, failures = s.execute(
            select(func.count(ToolCall.id),
                   func.sum(case((ToolCall.success.is_(False), 1), else_=0)))
            .join(AgentTask, AgentTask.id == ToolCall.task_id)
            .where(AgentTask.merchant_id == m, AgentTask.created_at >= since)
        ).one()
        calls = calls or 0
        failures = failures or 0

        # p50 in the database would be a percentile function three engines
        # spell differently. The row count here is small and bounded by the
        # window, so it is honest and portable to sort in Python.
        durations = sorted(
            d for (d,) in s.execute(
                select(AgentTask.duration_ms)
                .where(AgentTask.merchant_id == m, AgentTask.created_at >= since,
                       AgentTask.duration_ms.is_not(None))).all())

    p50 = durations[(len(durations) - 1) // 2] if durations else None

    return {
        "window_hours": window_hours,
        "gated": gated,
        "approved": approved,
        "rejected": rejected,
        "moved_minor": int(moved_minor),
        "tool_calls": calls,
        "tool_errors": int(failures),
        # None rather than 0.0 when nothing ran: a rate over zero calls is not
        # zero, it is unknown, and a strip cell that reads 0.0% would be a lie.
        "tool_error_rate": (failures / calls) if calls else None,
        "p50_duration_ms": p50,
        "signing_secret_is_development_default": DEV_SECRET_IN_USE,
    }


@app.get("/scenarios")
def list_scenarios():
    """The suite, including what each scenario actually asserts.

    The description is prose; `expect` is the contract. A reader deciding
    whether "106/106" means anything needs the second, and returning only the
    first left them opening the YAML to find out. Setup that changes what a
    scenario means — who runs it, an injected fault, a back-dated approval — is
    returned for the same reason.

    All of it is static configuration that ships in the repository. Nothing here
    is per-merchant or secret, which is why the route stays unauthenticated
    alongside the runner.
    """
    return [{
        "id": s.id, "category": s.category, "critical": s.critical,
        "description": s.description,
        "request": s.request,
        "principal": s.principal,
        "expect": s.expect.model_dump(exclude_defaults=True),
        "setup": {k: v for k, v in {
            "initial_state": s.initial_state or None,
            "fault": s.fault,
            "approve": s.approve,
            "approve_as": s.approve_as,
            "expire_approval": s.expire_approval or None,
            "reverify": s.reverify or None,
            "reconcile": s.reconcile or None,
            "repeat_request": s.repeat_request or None,
            "allowed_tools": s.allowed_tools,
            "budget": s.budget,
        }.items() if v is not None},
    } for s in load_scenarios()]


@app.post("/scenarios/{scenario_id}/run")
def run_one(scenario_id: str):
    scen = {s.id: s for s in load_scenarios()}
    if scenario_id not in scen:
        raise HTTPException(404, "Unknown scenario.")
    import uuid
    settings = get_settings()
    with session_scope() as s:
        res = run_scenario(s, scen[scenario_id], f"RUN_{uuid.uuid4().hex[:8].upper()}")
        return {
            "scenario_id": res.scenario_id, "passed": res.passed,
            "checks": res.checks, "metrics": res.metrics,
            # The task the scenario produced. Without it a failing scenario is
            # a verdict with no way to see how it was reached — the trace, the
            # policy decisions and the evidence all hang off this id.
            "task_id": res.task_id,
            # Which provider produced it. An owner can switch providers at
            # runtime, and a run under a language model is not comparable to
            # the published suite even before its live-state caveat.
            "provider": settings.resolved_llm_provider,
            "model": settings.llm_model if settings.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        }
