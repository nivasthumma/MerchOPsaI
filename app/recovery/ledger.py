"""Revenue-recovery measurement — MerchantOps §49, §50.

    revenue at risk  >=  recoverable  >=  attempted  >=  recovered + failed + unknown

§49 ends with the sentence this module exists to obey:

> The platform should never call the entire INR 4.72L "recovered."

Every figure here is arithmetic over rows, in one unit -- the share of a charge
attributable to its incident. Mixing gross charges into that chain is how a
total ends up larger than the thing it is a share of, and a merchant reads a
recovery number that flatters the system.

## What each figure is allowed to mean

    at_risk      what detection measured as lost to the incident
    recoverable  the part of that sitting on transactions we may act on
    attempted    the part we actually acted on
    recovered    money CONFIRMED to have moved, by independent verification
    failed       acted on, and the action verifiably did not take effect
    unknown      acted on, and we could not establish what happened

`recovered` is the strictest of the six. A refund counts once its verification
reads SUCCESS. A payment link counts only once the provider says the link was
PAID -- creating one is an attempt, not a recovery, and reporting it otherwise
was a real defect this module's first test caught.

`unknown` is not a rounding error to be folded into either neighbour. It is the
honest size of what the system does not know, and §33 exists to keep it visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from app.models import IncidentStatus


@dataclass
class RecoveryLedger:
    """The six figures of §49, plus what they are made of."""
    merchant_id: str
    at_risk_minor: int = 0
    recoverable_minor: int = 0
    attempted_minor: int = 0
    recovered_minor: int = 0
    failed_minor: int = 0
    unknown_minor: int = 0
    outstanding_minor: int = 0          # attempted, not yet resolved either way
    by_incident: list[dict] = field(default_factory=list)
    by_method: list[dict] = field(default_factory=list)

    @property
    def settled_minor(self) -> int:
        return self.recovered_minor + self.failed_minor + self.unknown_minor

    def invariants(self) -> list[str]:
        """The orderings §49 asserts. Returned rather than raised: a violated
        invariant is a reporting defect that must be visible, and a dashboard
        that refuses to render is a dashboard nobody can use to find out why."""
        broken = []
        if self.recoverable_minor > self.at_risk_minor:
            broken.append("recoverable exceeds at_risk")
        if self.attempted_minor > self.recoverable_minor:
            broken.append("attempted exceeds recoverable")
        if self.settled_minor > self.attempted_minor:
            broken.append("settled outcomes exceed attempted")
        if self.recovered_minor > self.attempted_minor:
            broken.append("recovered exceeds attempted")
        return broken

    def as_dict(self) -> dict:
        return {
            "merchant_id": self.merchant_id,
            "at_risk_minor": self.at_risk_minor,
            "recoverable_minor": self.recoverable_minor,
            "attempted_minor": self.attempted_minor,
            "recovered_minor": self.recovered_minor,
            "failed_minor": self.failed_minor,
            "unknown_minor": self.unknown_minor,
            "outstanding_minor": self.outstanding_minor,
            "by_incident": self.by_incident,
            "by_method": self.by_method,
            "invariants_broken": self.invariants(),
            "basis": (
                "Attributed exposure: each charge counted only to the extent the "
                "incident is responsible for it. `recovered` is money confirmed "
                "by independent verification — a payment link counts only once "
                "the provider reports it paid, never when it is merely sent."),
        }


# Open incidents only. A resolved incident's exposure is history, and carrying
# it forward would make the at-risk figure grow monotonically forever.
_OPEN = tuple(s.value for s in IncidentStatus
              if s not in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED))


def build_ledger(session, merchant_id: str) -> RecoveryLedger:
    led = RecoveryLedger(merchant_id=merchant_id)

    led.at_risk_minor = int(session.execute(text("""
        SELECT COALESCE(SUM(revenue_at_risk_minor), 0) FROM incidents
        WHERE merchant_id = :m AND status = ANY(:open)
    """), {"m": merchant_id, "open": list(_OPEN)}).scalar() or 0)

    row = session.execute(text("""
        SELECT
          COALESCE(SUM(attributed_amount_minor) FILTER (
              WHERE status <> 'INELIGIBLE'), 0)                       AS recoverable,
          COALESCE(SUM(attributed_amount_minor) FILTER (
              WHERE status IN ('ATTEMPTED','RECOVERED','FAILED','UNKNOWN')), 0) AS attempted,
          COALESCE(SUM(actual_recovery_minor), 0)                     AS recovered,
          COALESCE(SUM(attributed_amount_minor) FILTER (
              WHERE status = 'FAILED'), 0)                            AS failed,
          COALESCE(SUM(attributed_amount_minor) FILTER (
              WHERE status = 'UNKNOWN'), 0)                           AS unknown,
          COALESCE(SUM(attributed_amount_minor) FILTER (
              WHERE status = 'ATTEMPTED'), 0)                         AS outstanding
        FROM recovery_candidates WHERE merchant_id = :m
    """), {"m": merchant_id}).mappings().one()

    led.recoverable_minor = int(row["recoverable"])
    led.attempted_minor = int(row["attempted"])
    led.recovered_minor = int(row["recovered"])
    led.failed_minor = int(row["failed"])
    led.unknown_minor = int(row["unknown"])
    led.outstanding_minor = int(row["outstanding"])

    # §50: at risk BY INCIDENT and BY PAYMENT METHOD.
    led.by_incident = [dict(r) for r in session.execute(text("""
        SELECT i.id AS incident_id, i.incident_type, i.severity, i.status, i.title,
               i.revenue_at_risk_minor,
               COALESCE(SUM(c.attributed_amount_minor) FILTER (
                   WHERE c.status <> 'INELIGIBLE'), 0) AS recoverable_minor,
               COALESCE(SUM(c.actual_recovery_minor), 0) AS recovered_minor
        FROM incidents i LEFT JOIN recovery_candidates c ON c.incident_id = i.id
        WHERE i.merchant_id = :m AND i.status = ANY(:open)
        GROUP BY i.id, i.incident_type, i.severity, i.status, i.title,
                 i.revenue_at_risk_minor
        ORDER BY i.revenue_at_risk_minor DESC
    """), {"m": merchant_id, "open": list(_OPEN)}).mappings().all()]

    # Method comes from the incident's own signals for a degradation, and from
    # the affected payment otherwise — there is no single column to read.
    led.by_method = [dict(r) for r in session.execute(text("""
        SELECT COALESCE(p.method, 'unknown') AS method,
               COALESCE(SUM(c.attributed_amount_minor) FILTER (
                   WHERE c.status <> 'INELIGIBLE'), 0) AS recoverable_minor,
               COALESCE(SUM(c.actual_recovery_minor), 0) AS recovered_minor,
               COUNT(*) AS candidates
        FROM recovery_candidates c JOIN payments p ON p.id = c.payment_id
        WHERE c.merchant_id = :m
        GROUP BY p.method ORDER BY recoverable_minor DESC
    """), {"m": merchant_id}).mappings().all()]

    return led


def dashboard(session, merchant_id: str) -> dict:
    """§50. The ledger, plus incident counts and agent activity."""
    from app.models import TaskStatus

    incidents = {r["status"]: int(r["n"]) for r in session.execute(text("""
        SELECT status, COUNT(*) AS n FROM incidents WHERE merchant_id = :m
        GROUP BY status
    """), {"m": merchant_id}).mappings().all()}

    activity = session.execute(text("""
        SELECT COUNT(*) AS investigations,
               COALESCE(SUM(tool_call_count), 0) AS tool_calls,
               COUNT(*) FILTER (WHERE recommendation IS NOT NULL) AS recommendations,
               COUNT(*) FILTER (WHERE status = 'AWAITING_APPROVAL') AS awaiting_approval,
               COUNT(*) FILTER (WHERE model_requires_human) AS model_escalations
        FROM agent_tasks WHERE merchant_id = :m AND is_replay = false
    """), {"m": merchant_id}).mappings().one()

    escalated_plans = int(session.execute(text("""
        SELECT COUNT(*) FROM recovery_plans
        WHERE merchant_id = :m AND status IN ('ESCALATED','STOPPED')
    """), {"m": merchant_id}).scalar() or 0)

    return {
        "recovery": build_ledger(session, merchant_id).as_dict(),
        "incidents": {
            "by_status": incidents,
            "open": sum(n for s, n in incidents.items() if s in _OPEN),
            "resolved": incidents.get("RESOLVED", 0) + incidents.get("CLOSED", 0),
        },
        "agent_activity": {
            "investigations": int(activity["investigations"]),
            "tool_calls": int(activity["tool_calls"]),
            "recommendations": int(activity["recommendations"]),
            "awaiting_approval": int(activity["awaiting_approval"]),
            "escalations": int(activity["model_escalations"]) + escalated_plans,
        },
    }
