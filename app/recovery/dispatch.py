"""Handing a candidate to the existing execution path — MerchantOps §28, §29.

There is no bulk executor here, and that is the point. A recovery candidate is
dispatched as an ordinary agent task and then goes through the same policy,
approval, idempotency and verification gates as any other financial action. A
second way to move money would be a second thing to get right, and the first one
already took four ADRs.

What this module adds on top of that path is the part §27 and §28 require and an
ordinary task does not have: the bounds are checked immediately before dispatch,
and the answer is acted on rather than logged.
"""
from __future__ import annotations

from app.agent.runtime import AgentRuntime
from app.audit.trace import record_incident
from app.models import (
    AgentAction, CandidateStatus, Incident, PlanStatus, RecoveryCandidate,
    RecoveryPlan, VerificationState,
)
from app.policy.risk import assess
from app.recovery.stopping import Disposition, StopDecision, apply_stop, evaluate_stopping_rules
from app.tools.registry import REGISTRY


class RecoveryStopped(Exception):
    """A stopping rule refused the dispatch. Carries the decision so the caller
    reports *which* rule fired rather than a generic failure."""

    def __init__(self, decision: StopDecision):
        self.decision = decision
        super().__init__(decision.reason)


def executable_candidates(session, plan: RecoveryPlan) -> list[RecoveryCandidate]:
    return (session.query(RecoveryCandidate)
            .filter(RecoveryCandidate.plan_id == plan.id,
                    RecoveryCandidate.executable.is_(True),
                    RecoveryCandidate.status == CandidateStatus.ELIGIBLE)
            .order_by(RecoveryCandidate.rank).all())


def assess_candidate_risk(session, plan: RecoveryPlan, candidate: RecoveryCandidate):
    """Grade one candidate as part of its campaign.

    `bulk_size` is the number of executable candidates in the plan, not one.
    Grading each action as if it stood alone is precisely how a bulk campaign
    escapes the risk class §24 assigns it.
    """
    bulk = len(executable_candidates(session, plan))
    return assess(session, tool_name="request_refund",
                  declared=REGISTRY["request_refund"].risk_class.value,
                  merchant_id=plan.merchant_id,
                  arguments={"synthetic_payment_id": candidate.payment_id,
                             "amount_minor": candidate.amount_minor},
                  spec=REGISTRY["request_refund"], bulk_size=bulk)


def dispatch_candidate(session, plan: RecoveryPlan, candidate: RecoveryCandidate,
                       principal, *, provider_available: bool = True):
    """Check the bounds, then hand the candidate to the ordinary agent path."""
    if not candidate.executable:
        raise RecoveryStopped(StopDecision(
            Disposition.STOP, "not_executable",
            f"{candidate.intervention.value} has no tool in this build; "
            f"{candidate.id} is a recommendation, not an action."))

    risk = assess_candidate_risk(session, plan, candidate)
    decision = evaluate_stopping_rules(session, plan, candidate,
                                       risk_level=risk.level,
                                       provider_available=provider_available)
    if decision.should_stop:
        # Acted on, not logged. §28's whole point is that the campaign changes
        # course; a rule that fires and is merely recorded stopped nothing.
        apply_stop(session, plan, decision)
        if decision.disposition is Disposition.ESCALATE:
            candidate.status = CandidateStatus.SKIPPED
            candidate.ineligible_reason = decision.rule
            session.flush()
        raise RecoveryStopped(decision)

    request = (f"Refund payment {candidate.payment_id} amount "
               f"{candidate.amount_minor} as recovery for incident "
               f"{plan.incident_id} (candidate {candidate.id}).")
    out = AgentRuntime(session, principal).run(request, incident_id=plan.incident_id)

    candidate.task_id = out.task.id
    candidate.attempts += 1
    candidate.status = CandidateStatus.ATTEMPTED
    if plan.status is PlanStatus.DRAFT:
        plan.status = PlanStatus.ACTIVE
    session.flush()

    incident = session.get(Incident, plan.incident_id)
    if incident is not None:
        record_incident(session, incident, "recovery_dispatched", {
            "plan_id": plan.id, "candidate_id": candidate.id,
            "task_id": out.task.id, "payment_id": candidate.payment_id,
            "amount_minor": candidate.amount_minor,
            "risk": risk.as_dict()})
    return {"candidate": candidate, "task": out.task, "outcome": out,
            "risk": risk}


# --- outcome ---------------------------------------------------------------
_FROM_VERIFICATION = {
    VerificationState.SUCCESS: CandidateStatus.RECOVERED,
    VerificationState.FAILED: CandidateStatus.FAILED,
    VerificationState.PARTIAL: CandidateStatus.FAILED,
    VerificationState.UNKNOWN: CandidateStatus.UNKNOWN,
}


def settle_plan(session, plan: RecoveryPlan) -> dict:
    """Read each dispatched candidate's outcome back from its action.

    The candidate does not decide what happened; the verified action does.
    `actual_recovery_minor` is populated ONLY from a SUCCESS -- MerchantOps §49
    keeps expected and actual apart, and an UNKNOWN action has not been shown to
    have moved anything.
    """
    counts = {s.value: 0 for s in CandidateStatus}
    recovered = 0

    for cand in session.query(RecoveryCandidate).filter(
            RecoveryCandidate.plan_id == plan.id).all():
        if cand.task_id:
            action = (session.query(AgentAction)
                      .filter(AgentAction.task_id == cand.task_id)
                      .order_by(AgentAction.created_at.desc()).first())
            if action is not None:
                action.recovery_candidate_id = cand.id
                if action.verification_state is not None:
                    cand.status = _FROM_VERIFICATION.get(
                        action.verification_state, CandidateStatus.UNKNOWN)
                    cand.actual_recovery_minor = (
                        action.amount_minor
                        if action.verification_state is VerificationState.SUCCESS else 0)
        counts[cand.status.value] += 1
        recovered += cand.actual_recovery_minor

    outstanding = session.query(RecoveryCandidate).filter(
        RecoveryCandidate.plan_id == plan.id,
        RecoveryCandidate.status.in_([CandidateStatus.ELIGIBLE,
                                      CandidateStatus.ATTEMPTED])).count()
    if outstanding == 0 and plan.status in (PlanStatus.DRAFT, PlanStatus.ACTIVE):
        plan.status = PlanStatus.COMPLETED
    session.flush()

    return {"plan_id": plan.id, "status": plan.status.value,
            "by_status": counts, "actual_recovery_minor": recovered,
            "expected_recovery_minor": plan.expected_recovery_minor}
