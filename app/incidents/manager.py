"""Incident → investigation — MerchantOps §13, §20, §21.

Dispatches the bounded agent against an incident the detection engine already
created, then moves the incident based on **what the task did**, never on what
the model said.

## The invariant this module exists to hold

    task status  -> incident status        (deterministic, here)
    model prose  -> incident status        (never)

An agent that concludes "this is resolved" does not resolve anything. The task's
recorded status is the input to the lifecycle move, and that status is set by the
runtime from observable behaviour -- tool calls made, policy decisions returned,
budgets exhausted. This is MerchantOps §38's separation of agent state from
system state, applied to the incident.

## Scope boundary

Investigation stops at ROOT_CAUSE_IDENTIFIED. `RECOVERY_PLANNED` onwards belongs
to the recovery planner (MerchantOps §23), which is not built. The one exception
is an investigation that reaches a policy decision requiring approval: those
states are entered because policy genuinely evaluated and genuinely required a
human, not to make the chain look complete.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.agent.confidence import assess
from app.agent.runtime import AgentRuntime, Principal
from app.audit.trace import record_incident
from app.incidents.lifecycle import transition
from app.models import (
    Incident, IncidentEvidence, IncidentSeverity, IncidentStatus as S,
    IncidentType, TaskStatus,
)

# Where an investigation leaves the incident, by the task's recorded status.
# Anything not listed escalates: an unrecognised outcome is not a resolved one.
_OUTCOME: dict[TaskStatus, S] = {
    TaskStatus.COMPLETED: S.ROOT_CAUSE_IDENTIFIED,
    TaskStatus.AWAITING_APPROVAL: S.APPROVAL_REQUIRED,
    TaskStatus.ABORTED_BUDGET: S.ESCALATED,
    TaskStatus.FAILED: S.ESCALATED,
    TaskStatus.DENIED: S.ESCALATED,
    TaskStatus.REJECTED: S.ESCALATED,
}

# Intermediate states walked to reach APPROVAL_REQUIRED. Each is true when it is
# recorded: the planner ran, policy evaluated, policy required a human.
_TO_APPROVAL = (S.ROOT_CAUSE_IDENTIFIED, S.RECOVERY_PLANNED,
                S.POLICY_EVALUATING, S.APPROVAL_REQUIRED)


def build_investigation_request(incident: Incident) -> str:
    """The task text, constructed from the incident — MerchantOps §20.

    Every value interpolated here came out of a SQL aggregate, so none of it is
    untrusted merchant text and none needs quarantining. That is a property of
    *this* prompt, not a general licence: the moment an incident summarises a
    customer note, it must be wrapped like any other untrusted value.

    The FACT/TASK split is §20's context contract. Facts are stated as facts so
    the model is not asked to re-derive them, and is not free to contradict them.
    """
    facts = "\n".join(f"  {k}: {v}" for k, v in sorted(incident.signals.items()))
    return (
        f"INCIDENT {incident.id} ({incident.incident_type.value}, severity "
        f"{incident.severity.value})\n"
        f"{incident.title}\n\n"
        f"FACT (measured by the detection engine, not to be re-estimated):\n"
        f"{facts}\n\n"
        f"TASK\n"
        f"Investigate this incident using the available tools. Establish what is "
        f"driving it, cite the evidence you used, and state the impact. Do not "
        f"invent figures the tools did not return."
    )


def investigate(session, incident: Incident, principal: Principal,
                provider=None) -> dict:
    """Run the agent against an incident and move the incident accordingly."""
    if incident.merchant_id != principal.merchant_id:
        # Same rule as everywhere else: no cross-merchant reach, and no
        # distinction between "forbidden" and "absent" (MerchantOps §54).
        raise PermissionError("Incident belongs to another merchant.")

    if incident.status in (S.RESOLVED, S.CLOSED):
        raise ValueError(f"Incident {incident.id} is {incident.status.value}; "
                         "it is not open for investigation.")

    if incident.status is S.DETECTED:
        transition(session, incident, S.TRIAGED,
                   reason="Dispatched for investigation.", actor=principal.user_id)
    if incident.status is S.TRIAGED:
        transition(session, incident, S.INVESTIGATING,
                   reason="Agent investigation started.", actor=principal.user_id)

    request = build_investigation_request(incident)
    runtime = AgentRuntime(session, principal, provider=provider)
    # Bound at creation. Binding after the run would put none of the run's own
    # events on the incident's trace -- see the note in AgentRuntime.run.
    # §58: the incident's id ties detection, its lifecycle and every task it
    # dispatched into one trace.
    out = runtime.run(request, incident_id=incident.id,
                      correlation_id=incident.correlation_id)

    # MerchantOps v2 §33: the platform assesses its own confidence from the
    # evidence, before the incident moves anywhere. Computed here rather than
    # inside the runtime because the subject is the *incident's* case, which
    # includes evidence detection gathered before any agent ran -- the agent's
    # own view of itself is one input among several, and the narrowest one.
    assessment = assess(
        evidence=list(incident.evidence),
        tool_calls=list(out.task.tool_calls),
        model_confidence=out.task.agent_confidence,
        # v2 §18: how many OTHER detection rules independently saw this
        # episode. Recorded by the sweep that raised the incident.
        corroborating_rules=len(
            (incident.signals or {}).get("correlation", {})
            .get("corroborating_rules", [])),
    )
    incident.confidence_band = assessment.band.value
    incident.confidence_inputs = assessment.as_dict()
    session.flush()

    record_incident(session, incident, "incident_investigated", {
        "task_id": out.task.id, "task_status": out.status.value,
        "tool_calls": out.task.tool_call_count,
        "duration_ms": out.task.duration_ms,
        "confidence": assessment.band.value,
        "model_confidence": out.task.agent_confidence,
    })

    target = _OUTCOME.get(out.status, S.ESCALATED)
    if target is S.APPROVAL_REQUIRED:
        for step in _TO_APPROVAL:
            transition(session, incident, step,
                       reason=f"Investigation task {out.task.id} requires approval.",
                       actor="system", payload={"task_id": out.task.id})
    else:
        transition(session, incident, target,
                   reason=f"Investigation task {out.task.id} finished as {out.status.value}.",
                   actor="system", payload={"task_id": out.task.id})

    return {"incident": incident, "task": out.task, "outcome": out}


def raise_incident(session, *, merchant_id: str, incident_type: IncidentType,
                   severity: IncidentSeverity, title: str, summary: str,
                   detection_key: str, detection_rule: str,
                   detection_version: str, revenue_at_risk_minor: int = 0,
                   signals: dict | None = None,
                   evidence: list[dict] | None = None,
                   started_at: datetime | None = None) -> Incident | None:
    """Create an incident from outside the detection sweep, once.

    Returns None when `detection_key` is already taken, exactly as the sweep
    does. Callers that raise on a recurring condition -- a reconciliation
    mismatch re-observed on every webhook redelivery, say -- therefore get one
    incident rather than one per observation, without having to check first.
    """
    from sqlalchemy.exc import IntegrityError

    inc = Incident(
        id=f"INC_{uuid.uuid4().hex[:10].upper()}", merchant_id=merchant_id,
        incident_type=incident_type, severity=severity, status=S.DETECTED,
        title=title, summary=summary, detection_key=detection_key,
        revenue_at_risk_minor=revenue_at_risk_minor, signals=signals or {},
        detection_rule=detection_rule, detection_version=detection_version,
        correlation_id=f"COR_{uuid.uuid4().hex[:12].upper()}",
        started_at=started_at or datetime.now(timezone.utc),
    )
    sp = session.begin_nested()
    try:
        session.add(inc)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        return None

    for ev in (evidence or []):
        session.add(IncidentEvidence(
            id=f"IEV_{uuid.uuid4().hex[:10].upper()}", incident_id=inc.id,
            key=ev["key"], value={"v": ev["value"]}, source=ev["source"],
            untrusted=bool(ev.get("untrusted", False)),
        ))
    session.flush()
    record_incident(session, inc, "incident_detected", {
        "incident_type": inc.incident_type.value, "severity": inc.severity.value,
        "rule": inc.detection_rule, "detection_version": inc.detection_version,
        "revenue_at_risk_minor": inc.revenue_at_risk_minor,
        "correlation_id": inc.correlation_id, "signals": inc.signals,
    })
    return inc
