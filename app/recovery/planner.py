"""Recovery planner — MerchantOps §22, §23, §27.

    Incident
       -> affected transactions
       -> eligibility
       -> expected recovery
       -> risk
       -> intervention candidates

## Where planning stops

§23's flow ends at *candidates*. This module does not execute anything, and
there is no bulk executor anywhere: a candidate is acted on through §29's
existing single-action path, with the same policy, approval, idempotency and
verification gates as any other financial action. A second way to move money
would be a second thing to get right.

## Who owns the numbers

§22 is unambiguous -- the calculation engine owns the result and the LLM
explains it. Every figure here is arithmetic over rows. The model may recommend
*which* intervention (§23); it cannot alter what one is worth, and it is not
consulted anywhere in this file.

## Expected recovery is an estimate with a stated basis

§49 orders the figures: revenue at risk >= eligible recovery >= expected
recovery. Getting that ordering right is not presentation -- an eligible figure
larger than the at-risk figure claims a merchant can recover more than the
incident cost them, which is a lie a dashboard would tell confidently.

The first implementation summed the full failed volume and did exactly that:
INR 34,467 eligible against INR 29,261 at risk. The gap is real and has a name.
Detection computes at-risk as the value of the *excess* failures -- the ones
above the method's own baseline. But some payments fail on the best of days, and
their value was never at risk from this incident.

So the volume is attributed before it is counted:

    attributable    = min(1, revenue_at_risk / total_failed_volume)
    eligible        = eligible_failed_volume x attributable
    expected        = eligible x baseline_success_rate(method)

For a refund the attributable fraction is 1 and the rate is 1: a duplicate
charge is owed back in full, and multiplying a debt by a conversion probability
would understate it.

The basis string travels with the number so the two cannot be separated, and
§49 keeps expected and actual in different columns so they can never be reported
as one.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit.trace import record_incident
from app.config import get_settings
from app.models import (
    CandidateStatus, Incident, IncidentType, Intervention, PlanStatus,
    RecoveryCandidate, RecoveryPlan,
)

PLANNER_VERSION = "planner-v1"

# Which interventions this build can actually carry out. The other five are
# legitimate recommendations with no tool behind them yet (§18 adds them), and a
# candidate proposing one is recorded but never counted as actionable.
EXECUTABLE = frozenset({Intervention.REFUND})

# Incident type -> the intervention that fits it. Deterministic: which remedy
# suits which failure is a property of the failure, not a judgement call the
# model gets to make differently on each run.
#
# A degraded method gets PAYMENT_LINK rather than RETRY on purpose -- re-presenting
# a customer to the same rail that is currently failing is not a recovery, it is
# the same failure again.
_INTERVENTION = {
    IncidentType.PAYMENT_DEGRADATION: Intervention.PAYMENT_LINK,
    IncidentType.DUPLICATE_PAYMENT: Intervention.REFUND,
    IncidentType.RECONCILIATION_MISMATCH: Intervention.HUMAN_ESCALATION,
}


@dataclass
class PlanResult:
    plan: RecoveryPlan
    created: bool
    candidates: list[RecoveryCandidate]


def _baseline_rate(incident: Incident) -> float:
    """The method's own prior-period success rate, from the detection signals."""
    pct = (incident.signals or {}).get("baseline_success_rate_pct")
    return (float(pct) / 100.0) if pct is not None else 0.0


def _degradation_candidates(session, incident: Incident) -> list[dict]:
    """Failed payments of the degraded method inside the incident window."""
    signals = incident.signals or {}
    method = signals.get("method")
    if not method:
        return []
    rows = session.execute(text("""
        SELECT p.id, p.customer_id, p.amount_minor, c.contact_opted_out
        FROM payments p JOIN customers c ON c.id = p.customer_id
        WHERE p.merchant_id = :m AND p.method = :meth AND p.status = 'failed'
          AND p.created_at >= :start
        ORDER BY p.amount_minor DESC, p.id
    """), {"m": incident.merchant_id, "meth": method,
           "start": signals.get("window_start")}).mappings().all()
    return [dict(r) for r in rows]


def _duplicate_candidates(session, incident: Incident) -> list[dict]:
    """The excess captures. The earliest is the real payment and is never a
    candidate — refunding it would undo the sale."""
    targets = (incident.signals or {}).get("excess_payment_ids") or []
    if not targets:
        return []
    rows = session.execute(text("""
        SELECT p.id, p.customer_id,
               (p.amount_minor - p.amount_refunded_minor) AS amount_minor,
               c.contact_opted_out
        FROM payments p JOIN customers c ON c.id = p.customer_id
        WHERE p.id = ANY(:ids) AND p.merchant_id = :m
        ORDER BY p.created_at, p.id
    """), {"ids": list(targets), "m": incident.merchant_id}).mappings().all()
    return [dict(r) for r in rows]


def _eligibility(row: dict, intervention: Intervention) -> tuple[bool, str | None]:
    """Deterministic. Returns (eligible, reason_if_not)."""
    if int(row["amount_minor"]) <= 0:
        return False, "nothing_to_recover"
    # §28: a customer who has opted out of contact is not a candidate for an
    # intervention that contacts them. A refund is money owed back and reaches
    # them through the payment rail, not through marketing consent, so opt-out
    # does not block it.
    contacting = intervention in (Intervention.PAYMENT_LINK,
                                  Intervention.CUSTOMER_NOTIFICATION,
                                  Intervention.RETRY,
                                  Intervention.SUBSCRIPTION_RETRY)
    if contacting and row.get("contact_opted_out"):
        return False, "customer_opted_out"
    return True, None


def plan_recovery(session, incident: Incident, *, principal=None) -> PlanResult:
    """Build (or return) the recovery plan for one incident."""
    s = get_settings()
    intervention = _INTERVENTION.get(incident.incident_type, Intervention.HUMAN_ESCALATION)

    if incident.incident_type is IncidentType.PAYMENT_DEGRADATION:
        rows = _degradation_candidates(session, incident)
    elif incident.incident_type is IncidentType.DUPLICATE_PAYMENT:
        rows = _duplicate_candidates(session, incident)
    else:
        rows = []

    rate = _baseline_rate(incident)
    if intervention is Intervention.REFUND:
        # A refund returns a known amount. There is no conversion probability to
        # apply, and multiplying one by a success rate would understate a debt.
        rate, basis = 1.0, ("A refund returns a known amount; expected recovery is "
                            "the unrefunded balance.")
    else:
        basis = (f"Failed volume attributed to this incident, times the method's "
                 f"own prior-period success rate ({rate:.1%}). An estimate of "
                 f"conversion on re-presentation, not a commitment.")

    # Attribute the volume to the incident before counting any of it. See the
    # module docstring: total failed volume includes failures that would have
    # happened at baseline and were never at risk from this incident.
    total_volume = sum(int(r["amount_minor"]) for r in rows)
    if intervention is Intervention.REFUND or not total_volume:
        attributable = 1.0
    else:
        attributable = min(1.0, incident.revenue_at_risk_minor / total_volume)

    eligible_volume = 0
    graded: list[dict] = []
    for row in rows:
        ok, why = _eligibility(row, intervention)
        amount = int(row["amount_minor"])
        expected = int(round(amount * attributable * rate)) if ok else 0
        if ok:
            eligible_volume += amount
        graded.append({**row, "eligible": ok, "reason": why, "expected": expected})

    # Round ONCE, from the exact aggregate. Rounding each candidate and then
    # summing accumulates: thirty-three shares rounded up by half a paise each
    # is enough to put eligible above at-risk and invert §49's ordering.
    #
    # An earlier version clamped the total instead. That held the ordering and
    # hid the reason it might break -- with attribution disabled the clamp still
    # produced a valid-looking ordering, so no assertion could tell a correct
    # figure from a wrong one that had been trimmed to fit. A mutation that
    # claimed the whole failed volume was at risk survived the entire suite.
    # Computing it correctly once is both the better arithmetic and the version
    # that can be checked.
    eligible_exact = eligible_volume * attributable
    eligible_minor = int(round(eligible_exact))
    expected_minor = int(round(eligible_exact * rate))

    graded.sort(key=lambda r: (not r["eligible"], -r["expected"], r["id"]))

    now = datetime.now(timezone.utc)
    plan = RecoveryPlan(
        id=f"RPL_{uuid.uuid4().hex[:10].upper()}",
        incident_id=incident.id, merchant_id=incident.merchant_id,
        status=PlanStatus.DRAFT, intervention=intervention,
        # One plan per incident. A second pass refines this plan; it does not
        # open a parallel campaign with its own separate budget.
        plan_key=f"{incident.merchant_id}|{incident.id}",
        revenue_at_risk_minor=incident.revenue_at_risk_minor,
        eligible_recovery_minor=eligible_minor,
        expected_recovery_minor=expected_minor,
        expected_recovery_basis=basis,
        max_recovery_minor=s.recovery_max_amount_minor,
        max_actions=s.recovery_max_actions,
        max_attempts_per_customer=s.recovery_max_attempts_per_customer,
        max_duration_seconds=s.recovery_max_duration_seconds,
        planner_version=PLANNER_VERSION,
        correlation_id=incident.correlation_id,
        expires_at=now + timedelta(seconds=s.recovery_max_duration_seconds),
    )

    sp = session.begin_nested()
    try:
        session.add(plan)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        existing = session.query(RecoveryPlan).filter(
            RecoveryPlan.plan_key == f"{incident.merchant_id}|{incident.id}").one()
        return PlanResult(existing, False, list(existing.candidates))

    candidates: list[RecoveryCandidate] = []
    for rank, row in enumerate(graded, start=1):
        candidates.append(RecoveryCandidate(
            id=f"RCD_{uuid.uuid4().hex[:10].upper()}", plan_id=plan.id,
            incident_id=incident.id, merchant_id=incident.merchant_id,
            payment_id=row["id"], customer_id=row["customer_id"],
            amount_minor=int(row["amount_minor"]), intervention=intervention,
            status=CandidateStatus.ELIGIBLE if row["eligible"] else CandidateStatus.INELIGIBLE,
            ineligible_reason=row["reason"],
            expected_recovery_minor=row["expected"],
            executable=(intervention in EXECUTABLE) and row["eligible"],
            rank=rank,
        ))
    session.add_all(candidates)
    session.flush()

    record_incident(session, incident, "recovery_planned", {
        "plan_id": plan.id, "intervention": intervention.value,
        "candidates": len(candidates),
        "eligible": sum(1 for c in candidates if c.status is CandidateStatus.ELIGIBLE),
        "executable": sum(1 for c in candidates if c.executable),
        "eligible_recovery_minor": eligible_minor,
        "expected_recovery_minor": expected_minor,
        "expected_recovery_basis": basis,
        "budget": {
            "max_recovery_minor": plan.max_recovery_minor,
            "max_actions": plan.max_actions,
            "max_attempts_per_customer": plan.max_attempts_per_customer,
            "max_duration_seconds": plan.max_duration_seconds,
        },
        "planner_version": PLANNER_VERSION,
    })
    return PlanResult(plan, True, candidates)
