"""Operational metrics and SLOs — MerchantOps §59, §60.

§59 names fourteen measurements. Some are arithmetic over rows this system
already has. Some need ground truth it does not have, and those are reported as
**unavailable with a reason** rather than approximated.

That distinction is the whole design. A dashboard showing "root-cause accuracy:
94%" computed from nothing is worse than one showing a blank, because the blank
prompts the question and the number closes it. Every metric here carries
`available` and, when false, why — so a reader can tell a measurement from a
gap, which is the one thing an aggregate figure cannot say about itself.

§60's objectives are checked the same way: an SLO nobody is timing is a wish,
so each one reports its measured value and whether it holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

# §60. Stated here rather than inline so the objective and the check cannot
# drift apart.
SLO_DETECTION_MS = 60_000
SLO_POLICY_DECISION_MS = 200


@dataclass
class Metric:
    name: str
    value: float | int | None
    unit: str
    available: bool = True
    reason: str = ""
    sample_size: int = 0

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "available": self.available, "reason": self.reason,
                "sample_size": self.sample_size}


@dataclass
class Objective:
    name: str
    target: str
    measured: float | int | None
    holds: bool | None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "target": self.target, "measured": self.measured,
                "holds": self.holds, "detail": self.detail}


def _percentile(session, sql: str, params: dict, pct: float = 0.5) -> tuple[float | None, int]:
    row = session.execute(text(f"""
        SELECT percentile_disc(:p) WITHIN GROUP (ORDER BY v) AS v, COUNT(*) AS n
        FROM ({sql}) s
    """), {**params, "p": pct}).mappings().one()
    return (float(row["v"]) if row["v"] is not None else None), int(row["n"])


def operational_metrics(session, merchant_id: str) -> dict:
    """§59, honestly split into what is measured and what is not."""
    from app.recovery.ledger import build_ledger

    m: list[Metric] = []

    # --- latencies, all from durations already recorded -------------------
    v, n = _percentile(session, """
        SELECT (payload->>'duration_ms')::float AS v FROM audit_logs
        WHERE merchant_id = :m AND event_type = 'incident_detected'
          AND payload->>'duration_ms' IS NOT NULL
    """, {"m": merchant_id})
    m.append(Metric("detection_latency_p50", v, "ms", v is not None,
                    "" if v is not None else "No detection sweep has been recorded.", n))

    v, n = _percentile(session, """
        SELECT duration_ms::float AS v FROM agent_tasks
        WHERE merchant_id = :m AND duration_ms IS NOT NULL AND is_replay = false
    """, {"m": merchant_id})
    m.append(Metric("investigation_latency_p50", v, "ms", v is not None,
                    "" if v is not None else "No task has completed.", n))

    v, n = _percentile(session, """
        SELECT tc.duration_ms::float AS v FROM tool_calls tc
        JOIN agent_tasks t ON t.id = tc.task_id WHERE t.merchant_id = :m
    """, {"m": merchant_id})
    m.append(Metric("tool_latency_p50", v, "ms", v is not None,
                    "" if v is not None else "No tool has been called.", n))

    v, n = _percentile(session, """
        SELECT (payload->>'duration_ms')::float AS v FROM audit_logs
        WHERE merchant_id = :m AND event_type IN ('policy_decision','policy_recheck')
          AND payload->>'duration_ms' IS NOT NULL
    """, {"m": merchant_id}, pct=0.95)
    m.append(Metric("policy_decision_p95", v, "ms", v is not None,
                    "" if v is not None else "No policy decision has been recorded.", n))

    v, n = _percentile(session, """
        SELECT provider_latency_ms AS v FROM agent_actions
        WHERE merchant_id = :m AND provider_latency_ms IS NOT NULL
    """, {"m": merchant_id})
    m.append(Metric("provider_latency_p50", v, "ms", v is not None,
                    "" if v is not None else "No provider call has been made.", n))

    v, n = _percentile(session, """
        SELECT verification_latency_ms AS v FROM agent_actions
        WHERE merchant_id = :m AND verification_latency_ms IS NOT NULL
    """, {"m": merchant_id})
    m.append(Metric("verification_latency_p50", v, "ms", v is not None,
                    "" if v is not None else "No action has been verified.", n))

    # --- rates and integrity ----------------------------------------------
    row = session.execute(text("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE verification_state = 'UNKNOWN') AS unknown,
               COUNT(*) FILTER (WHERE approval_id IS NULL) AS unapproved
        FROM agent_actions WHERE merchant_id = :m
    """), {"m": merchant_id}).mappings().one()
    total = int(row["total"])
    m.append(Metric("unknown_rate", round(int(row["unknown"]) / total, 4) if total else None,
                    "ratio", total > 0,
                    "" if total else "No external action has been attempted.", total))

    # §59's "policy violations" and "unauthorized actions" should be zero by
    # construction. Computing them anyway is the point: a control that is only
    # ever asserted in tests is a control nobody is watching in production.
    m.append(Metric("actions_without_an_approval", int(row["unapproved"]), "count",
                    True, "", total))
    violations = int(session.execute(text("""
        SELECT COUNT(*) FROM agent_actions a
        LEFT JOIN approvals ap ON ap.id = a.approval_id
        WHERE a.merchant_id = :m AND (ap.id IS NULL OR ap.decision <> 'APPROVED')
    """), {"m": merchant_id}).scalar() or 0)
    m.append(Metric("policy_violations", violations, "count", True, "", total))

    # --- recovery ----------------------------------------------------------
    led = build_ledger(session, merchant_id)
    m.append(Metric("actual_revenue_recovered", led.recovered_minor, "minor", True, "", 0))
    m.append(Metric("recovery_rate",
                    round(led.recovered_minor / led.recoverable_minor, 4)
                    if led.recoverable_minor else None,
                    "ratio", led.recoverable_minor > 0,
                    "" if led.recoverable_minor else "Nothing is recoverable yet.", 0))
    m.append(Metric("recovery_precision",
                    round(led.recovered_minor / led.attempted_minor, 4)
                    if led.attempted_minor else None,
                    "ratio", led.attempted_minor > 0,
                    "" if led.attempted_minor else "Nothing has been attempted yet.", 0))

    # --- the ones that need ground truth this system does not have --------
    m.append(Metric(
        "root_cause_accuracy", None, "ratio", False,
        "Requires labelled incidents: a recorded true cause to compare the agent's "
        "conclusion against. The evaluation suite has expected answers per scenario "
        "and can measure this; a production incident has no label, so nothing here "
        "can. Reported as unavailable rather than approximated.", 0))
    m.append(Metric(
        "revenue_at_risk_accuracy", None, "ratio", False,
        "Requires knowing what was actually lost, which is only knowable after the "
        "fact and is not recorded. The seeded dataset knows its own planted "
        "degradation; a real merchant's data does not.", 0))
    m.append(Metric(
        "agent_cost", None, "currency", False,
        "No token accounting on this path. The deterministic planner has no "
        "tokeniser and the Anthropic provider has never executed, so any figure "
        "here would be invented.", 0))

    return {
        "merchant_id": merchant_id,
        "available": [x.as_dict() for x in m if x.available],
        "unavailable": [x.as_dict() for x in m if not x.available],
        "note": ("A metric is either measured or absent. Nothing here is estimated, "
                 "and the reason a figure is missing is more useful than a figure "
                 "that was guessed."),
    }


def objectives(session, merchant_id: str) -> list[dict]:
    """§60. Each objective reports what was measured and whether it holds."""
    out: list[Objective] = []

    v, n = _percentile(session, """
        SELECT (payload->>'duration_ms')::float AS v FROM audit_logs
        WHERE merchant_id = :m AND event_type = 'incident_detected'
          AND payload->>'duration_ms' IS NOT NULL
    """, {"m": merchant_id}, pct=0.95)
    out.append(Objective("detection_latency", f"< {SLO_DETECTION_MS} ms (p95)", v,
                         None if v is None else v < SLO_DETECTION_MS,
                         "No detection sweep recorded." if v is None else f"{n} sample(s)"))

    v, n = _percentile(session, """
        SELECT (payload->>'duration_ms')::float AS v FROM audit_logs
        WHERE merchant_id = :m AND event_type IN ('policy_decision','policy_recheck')
          AND payload->>'duration_ms' IS NOT NULL
    """, {"m": merchant_id}, pct=0.95)
    out.append(Objective("policy_decision_latency", f"< {SLO_POLICY_DECISION_MS} ms (p95)", v,
                         None if v is None else v < SLO_POLICY_DECISION_MS,
                         "No policy decision recorded." if v is None else f"{n} sample(s)"))

    # The two correctness objectives. §60 calls these the most important, and
    # they are counts rather than latencies: the target is zero, not "low".
    unauthorized = int(session.execute(text("""
        SELECT COUNT(*) FROM agent_actions a
        LEFT JOIN approvals ap ON ap.id = a.approval_id
        WHERE a.merchant_id = :m AND (ap.id IS NULL OR ap.decision <> 'APPROVED')
    """), {"m": merchant_id}).scalar() or 0)
    out.append(Objective("unauthorized_executions", "0", unauthorized, unauthorized == 0,
                         "Every external action must trace to an approved approval."))

    unverified = int(session.execute(text("""
        SELECT COUNT(*) FROM agent_actions
        WHERE merchant_id = :m AND status = 'CONFIRMED'
          AND (verification_state IS NULL OR verification_state <> 'SUCCESS')
    """), {"m": merchant_id}).scalar() or 0)
    out.append(Objective("unverified_success_claims", "0", unverified, unverified == 0,
                         "No action may be CONFIRMED without an independent read-back "
                         "that says SUCCESS."))

    return [o.as_dict() for o in out]
