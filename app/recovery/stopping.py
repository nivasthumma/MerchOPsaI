"""Stopping rules and budget enforcement — MerchantOps §27, §28.

§28 opens with the line this module exists to honour:

> Stopping is a first-class capability.

The wrong answer to "this is not working" is another attempt. Every rule here
returns an explicit disposition -- CONTINUE, STOP or ESCALATE -- and the caller
must act on it. A rule that fired and was logged is not a rule that stopped
anything.

## STOP versus ESCALATE

They are different claims, and collapsing them loses the one that matters.

    STOP      the campaign is finished or not worth continuing.
              Nothing is wrong. Nobody needs to be woken up.

    ESCALATE  the campaign cannot safely decide for itself.
              A human has to look.

Budget exhaustion is a STOP: the bound did its job. A provider that has gone
away, or evidence that does not support the plan, is an ESCALATE: the system has
encountered something it is not entitled to resolve alone.

## Why the budget is checked before every action and not once

§27's bounds are on a campaign that runs over time. Checking them at planning
time only tells you what was true at planning time -- actions land, attempts
accumulate, and the clock runs. `check_budget` is therefore called immediately
before each execution, and the answer it gives is about the state right now.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import get_settings
from app.models import (
    RISK_ORDER,
    CandidateStatus,
    PlanStatus,
    RecoveryCandidate,
    RecoveryPlan,
)


class Disposition(str, enum.Enum):
    CONTINUE = "CONTINUE"
    STOP = "STOP"
    ESCALATE = "ESCALATE"


@dataclass
class StopDecision:
    disposition: Disposition
    rule: str | None = None
    reason: str = ""
    details: dict = field(default_factory=dict)

    @property
    def should_stop(self) -> bool:
        return self.disposition is not Disposition.CONTINUE

    def as_dict(self) -> dict:
        return {"disposition": self.disposition.value, "rule": self.rule,
                "reason": self.reason, "details": self.details}


_CONTINUE = StopDecision(Disposition.CONTINUE)

# §28: "Risk exceeds allowed level". A campaign may not carry out an action
# graded above this without a human; CRITICAL work escalates rather than running.
MAX_UNATTENDED_RISK = "HIGH"


def _spent_and_attempted(session, plan: RecoveryPlan) -> tuple[int, int]:
    """(recovery amount committed, actions taken) so far on this plan.

    Committed counts everything ATTEMPTED as well as RECOVERED. A budget that
    only counted confirmed successes would let an unbounded number of failures
    through, which is the opposite of a bound.
    """
    row = session.execute(text("""
        SELECT COALESCE(SUM(amount_minor), 0) AS spent, COUNT(*) AS n
        FROM recovery_candidates
        WHERE plan_id = :p AND status IN ('ATTEMPTED','RECOVERED','FAILED','UNKNOWN')
    """), {"p": plan.id}).mappings().one()
    return int(row["spent"]), int(row["n"])


def check_budget(session, plan: RecoveryPlan,
                 candidate: RecoveryCandidate | None = None) -> StopDecision:
    """MerchantOps §27. Called immediately before an action, never once."""
    now = datetime.now(UTC)

    expires = plan.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if now >= expires:
        return StopDecision(Disposition.STOP, "max_duration_exceeded",
                            f"Plan {plan.id} passed its {plan.max_duration_seconds}s "
                            f"maximum duration at {expires.isoformat()}.",
                            {"expires_at": expires.isoformat()})

    spent, taken = _spent_and_attempted(session, plan)

    if taken >= plan.max_actions:
        return StopDecision(Disposition.STOP, "max_actions_reached",
                            f"Plan {plan.id} has taken {taken} of a maximum "
                            f"{plan.max_actions} actions.",
                            {"actions_taken": taken, "max_actions": plan.max_actions})

    if candidate is not None:
        # The prospective spend, not the spend so far. Checking after the fact
        # is not a limit, it is a report.
        prospective = spent + candidate.amount_minor
        if prospective > plan.max_recovery_minor:
            return StopDecision(
                Disposition.STOP, "recovery_budget_exhausted",
                f"Acting on {candidate.id} would commit {prospective / 100:,.2f}, "
                f"over the plan's {plan.max_recovery_minor / 100:,.2f} maximum.",
                {"committed_minor": spent, "prospective_minor": prospective,
                 "max_recovery_minor": plan.max_recovery_minor})

        per_customer = session.execute(text("""
            SELECT COALESCE(SUM(attempts), 0) FROM recovery_candidates
            WHERE plan_id = :p AND customer_id = :c
        """), {"p": plan.id, "c": candidate.customer_id}).scalar() or 0
        if per_customer >= plan.max_attempts_per_customer:
            return StopDecision(
                Disposition.STOP, "max_attempts_per_customer",
                f"Customer {candidate.customer_id} has already been approached "
                f"{per_customer} time(s); the plan allows "
                f"{plan.max_attempts_per_customer}.",
                {"attempts": per_customer,
                 "max_attempts_per_customer": plan.max_attempts_per_customer})

    return _CONTINUE


def evaluate_stopping_rules(session, plan: RecoveryPlan,
                            candidate: RecoveryCandidate | None = None,
                            *, risk_level: str | None = None,
                            provider_available: bool = True) -> StopDecision:
    """MerchantOps §28, in order. The first rule that fires wins."""
    s = get_settings()

    if plan.status in (PlanStatus.STOPPED, PlanStatus.ESCALATED, PlanStatus.EXPIRED):
        return StopDecision(Disposition.STOP, plan.stop_rule or "already_stopped",
                            plan.stop_reason or f"Plan {plan.id} is {plan.status.value}.")

    # --- provider unavailable -> ESCALATE --------------------------------
    # Not a STOP. The campaign was viable a moment ago and the obstacle is
    # outside it, so this is a condition someone should see rather than a
    # decision the plan is entitled to make.
    if not provider_available:
        return StopDecision(Disposition.ESCALATE, "provider_unavailable",
                            "The payment provider is not reachable; recovery cannot "
                            "proceed and must not be retried blindly.")

    # --- risk exceeds the allowed level -> ESCALATE ----------------------
    if risk_level and RISK_ORDER.get(risk_level, 0) > RISK_ORDER[MAX_UNATTENDED_RISK]:
        return StopDecision(Disposition.ESCALATE, "risk_exceeds_allowed_level",
                            f"An action graded {risk_level} is above the {MAX_UNATTENDED_RISK} "
                            f"ceiling for automated recovery; it needs a human.",
                            {"risk_level": risk_level, "ceiling": MAX_UNATTENDED_RISK})

    # --- evidence insufficient -> ESCALATE -------------------------------
    # A plan with no candidates at all means the detection signals did not
    # resolve to any transaction. That is not "nothing to do" -- it is a plan
    # whose premise could not be substantiated.
    total = session.query(RecoveryCandidate).filter(
        RecoveryCandidate.plan_id == plan.id).count()
    if total == 0:
        return StopDecision(Disposition.ESCALATE, "evidence_insufficient",
                            f"Incident {plan.incident_id} produced no recovery "
                            f"candidates; its signals did not resolve to any "
                            f"transaction this plan can act on.")

    # --- budget (§27) ----------------------------------------------------
    budget = check_budget(session, plan, candidate)
    if budget.should_stop:
        return budget

    # --- expected recovery below threshold -> STOP -----------------------
    remaining = session.execute(text("""
        SELECT COALESCE(SUM(expected_recovery_minor), 0) FROM recovery_candidates
        WHERE plan_id = :p AND status = 'ELIGIBLE'
    """), {"p": plan.id}).scalar() or 0
    if remaining < s.recovery_min_expected_minor:
        return StopDecision(
            Disposition.STOP, "expected_recovery_below_threshold",
            f"Remaining expected recovery {remaining / 100:,.2f} is below the "
            f"{s.recovery_min_expected_minor / 100:,.2f} threshold; further "
            f"attempts cost more than they return.",
            {"remaining_minor": remaining,
             "threshold_minor": s.recovery_min_expected_minor})

    # --- customer opted out (per candidate) -> STOP ----------------------
    if candidate is not None and candidate.status is CandidateStatus.INELIGIBLE:
        return StopDecision(Disposition.STOP, "candidate_ineligible",
                            f"{candidate.id} is ineligible: "
                            f"{candidate.ineligible_reason}.",
                            {"reason": candidate.ineligible_reason})

    return _CONTINUE


def apply_stop(session, plan: RecoveryPlan, decision: StopDecision) -> RecoveryPlan:
    """Record a stop on the plan. Separate from evaluation so that a caller
    asking "may I proceed?" cannot accidentally halt a campaign by asking."""
    from app.audit.trace import record_incident
    from app.models import Incident

    if decision.disposition is Disposition.CONTINUE:
        return plan

    plan.status = (PlanStatus.ESCALATED
                   if decision.disposition is Disposition.ESCALATE
                   else PlanStatus.STOPPED)
    plan.stop_rule = decision.rule
    plan.stop_reason = decision.reason
    session.flush()

    incident = session.get(Incident, plan.incident_id)
    if incident is not None:
        record_incident(session, incident, "recovery_stopped", {
            "plan_id": plan.id, **decision.as_dict()})
    return plan
