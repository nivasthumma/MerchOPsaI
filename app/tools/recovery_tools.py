"""calculate_recovery_candidates — MerchantOps §18, §22, §23.

A calculation, not a commitment. It writes nothing: §23's flow ends at
candidates, and persisting a plan is the application's decision, not the
model's.

It calls the same `compute_plan` the planner persists from. Computing figures
separately here would let the model be told one number while the system stored
another, and the model's answer is what a merchant reads.
"""
from __future__ import annotations

from app.models import Incident
from app.tools.contracts import Evidence, RiskClass, ToolResult, ToolSpec

SPEC_RECOVERY_CANDIDATES = ToolSpec(
    name="calculate_recovery_candidates",
    description=(
        "For an incident, compute which transactions could be recovered, which "
        "are eligible, what intervention fits, and what recovery is expected to "
        "be worth. Read-only: this proposes nothing and executes nothing. The "
        "expected figure is an estimate and is returned with its basis."
    ),
    input_schema={
        "type": "object",
        "properties": {"incident_id": {"type": "string", "description": "e.g. INC_XXXXXXXX"}},
        "required": ["incident_id"],
    },
    required_permissions=["read:metrics"],
    risk_class=RiskClass.LOW,
)


def calculate_recovery_candidates(session, merchant_id: str, incident_id: str) -> ToolResult:
    from app.recovery.planner import compute_plan

    incident = session.get(Incident, incident_id)
    if incident is None or incident.merchant_id != merchant_id:
        # No distinction between absent and another merchant's (§54).
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"incident_id": incident_id}, risk_level="LOW")

    draft = compute_plan(session, incident)
    data = {"incident_id": incident.id, "incident_type": incident.incident_type.value,
            "revenue_at_risk_minor": incident.revenue_at_risk_minor, **draft.as_dict()}

    def inr(minor: int) -> str:
        return f"INR {minor / 100:,.2f}"

    ev = [
        Evidence(key="intervention", value=draft.intervention.value, source="recovery_planner"),
        Evidence(key="eligible_candidates", value=data["eligible_count"], source="recovery_planner"),
        Evidence(key="revenue_at_risk", value=inr(incident.revenue_at_risk_minor),
                 source="calculation_engine"),
        Evidence(key="eligible_recovery", value=inr(draft.eligible_recovery_minor),
                 source="calculation_engine"),
        # The estimate never travels without its basis.
        Evidence(key="expected_recovery",
                 value=f"{inr(draft.expected_recovery_minor)} — {draft.basis}",
                 source="calculation_engine"),
    ]
    if not data["executable"]:
        ev.append(Evidence(
            key="executable",
            value=(f"{draft.intervention.value} has no tool in this build; these are "
                   f"recommendations, not actions."),
            source="recovery_planner"))
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")
