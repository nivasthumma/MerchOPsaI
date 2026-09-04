"""The campaign view of a recovery plan — MerchantOps v2 §37, §38.

v2 §37 draws a campaign as a card:

    RC-017
    Objective:                Recover failed payments
    Affected:                 1,842
    Eligible:                 1,126
    Expected recovery:        ₹3.4L
    Budget:                   ₹4L
    Maximum attempts/customer: 2
    Status:                   ACTIVE

## There is no campaign table, on purpose

`RecoveryPlan` is already this. It carries the objective (`intervention`), the
computed figures (§22), all of §38's bounds, the stopping rule that ended it
(§28), and a status. Its candidates are the affected transactions. Adding a
`campaigns` table beside it would be two records of one thing, with two places
for a budget to be enforced and one of them eventually forgotten -- the shape
v2 §103 warns against when it says enterprise grade "does not mean more
microservices, more dashboards".

So this module is a projection, not an entity. It computes the counts and the
budget *consumption* that §37's card needs and the plan does not store, because
those are derived from candidates and would go stale the moment one moved.

## What was actually missing

Two things, and neither was a table:

1. **Budget consumption.** §37 shows a budget beside an expected recovery, but
   a merchant watching an ACTIVE campaign needs to know how much of it is
   already spent. `check_budget` computed that internally, per decision, and
   threw it away; nothing could show it.
2. **The affected/eligible split as figures.** The candidates carry it and
   nobody added them up.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from app.models import PlanStatus, RecoveryPlan

# §37's objective line, in the merchant's words rather than the enum's.
OBJECTIVE = {
    "RETRY": "Retry failed payments",
    "PAYMENT_LINK": "Recover failed payments by payment link",
    "CUSTOMER_NOTIFICATION": "Notify affected customers",
    "SUBSCRIPTION_RETRY": "Retry failed subscription charges",
    "REFUND": "Refund duplicate charges",
    "HUMAN_ESCALATION": "Escalate for manual handling",
    "NO_ACTION": "No automated recovery",
}


def summary(session, plan: RecoveryPlan) -> dict:
    """§37's card for one campaign, computed from its candidates.

    Every figure is derived at read time rather than cached on the plan. A
    cached count is a count that disagrees with the rows the first time a
    candidate moves, and these are numbers a merchant reads while the campaign
    is running.
    """
    counts = session.execute(text("""
        SELECT status, COUNT(*) AS n,
               COALESCE(SUM(attributed_amount_minor), 0) AS attributed,
               COALESCE(SUM(actual_recovery_minor), 0)   AS actual
        FROM recovery_candidates
        WHERE plan_id = :p
        GROUP BY status
    """), {"p": plan.id}).mappings().all()

    by_status = {r["status"]: r for r in counts}
    total = sum(r["n"] for r in counts)

    def n(*statuses: str) -> int:
        return sum(by_status[s]["n"] for s in statuses if s in by_status)

    # What the campaign has committed so far. Attempted work counts against the
    # budget whether or not it succeeded -- an attempt that failed still spent
    # an action and still reached a customer, and a budget that only counted
    # successes would let a campaign retry indefinitely at no recorded cost.
    spent_minor = sum(by_status[s]["attributed"] for s in
                      ("ATTEMPTED", "RECOVERED", "FAILED", "UNKNOWN")
                      if s in by_status)
    actions_taken = n("ATTEMPTED", "RECOVERED", "FAILED", "UNKNOWN")
    recovered_minor = sum(by_status[s]["actual"] for s in by_status)

    started = plan.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = int((datetime.now(UTC) - started).total_seconds())

    return {
        "id": plan.id,
        "incident_id": plan.incident_id,
        "objective": OBJECTIVE.get(plan.intervention.value, plan.intervention.value),
        "intervention": plan.intervention.value,
        "status": plan.status.value,

        # §37's affected / eligible split.
        "affected": total,
        "eligible": n("ELIGIBLE"),
        "ineligible": n("INELIGIBLE"),
        "attempted": actions_taken,
        "recovered": n("RECOVERED"),
        "failed": n("FAILED"),
        "unknown": n("UNKNOWN"),
        "skipped": n("SKIPPED"),

        # §22 owns these; they are read, never recomputed here.
        "revenue_at_risk_minor": plan.revenue_at_risk_minor,
        "eligible_recovery_minor": plan.eligible_recovery_minor,
        "expected_recovery_minor": plan.expected_recovery_minor,
        "expected_recovery_basis": plan.expected_recovery_basis,
        # §49 keeps expected and actual apart, and so does this.
        "recovered_minor": recovered_minor,

        # §38's five bounds, each beside what has been used against it. A limit
        # with no consumption reading is a limit nobody can see approaching.
        "budget": {
            "max_recovery_minor": plan.max_recovery_minor,
            "spent_minor": spent_minor,
            "max_actions": plan.max_actions,
            "actions_taken": actions_taken,
            "max_attempts_per_customer": plan.max_attempts_per_customer,
            "max_duration_seconds": plan.max_duration_seconds,
            "elapsed_seconds": elapsed,
            "max_risk_level": plan.max_risk_level,
        },
        "exhausted": _exhausted(plan, spent_minor, actions_taken, elapsed),

        "stop_rule": plan.stop_rule,
        "stop_reason": plan.stop_reason,
        "expires_at": plan.expires_at.isoformat(),
    }


def _exhausted(plan: RecoveryPlan, spent_minor: int, actions_taken: int,
               elapsed: int) -> list[str]:
    """Which bounds are already used up.

    Reported rather than acted on. `evaluate_stopping_rules` is the authority on
    whether a campaign may continue, and a second place that decides the same
    question is a second place for the two to disagree. This says what a reader
    would otherwise have to work out by comparing eight numbers.
    """
    out = []
    if spent_minor >= plan.max_recovery_minor:
        out.append("max_recovery_minor")
    if actions_taken >= plan.max_actions:
        out.append("max_actions")
    if elapsed >= plan.max_duration_seconds:
        out.append("max_duration_seconds")
    return out


def active_campaigns(session, merchant_id: str) -> list[RecoveryPlan]:
    """Campaigns still capable of doing something, for the operations console.

    STOPPED, ESCALATED, EXPIRED and COMPLETED are excluded for the same reason
    `open_incidents` excludes RESOLVED: a console listing finished work beside
    live work is a console nobody reads.
    """
    from sqlalchemy import select

    return list(session.execute(
        select(RecoveryPlan)
        .where(RecoveryPlan.merchant_id == merchant_id,
               RecoveryPlan.status.in_([PlanStatus.DRAFT, PlanStatus.ACTIVE]))
        .order_by(RecoveryPlan.expected_recovery_minor.desc())
    ).scalars().all())
