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
from app.incidents.lifecycle import advance as _advance
from app.models import (
    AgentAction,
    CandidateStatus,
    Incident,
    Intervention,
    PlanStatus,
    RecoveryCandidate,
    RecoveryPlan,
    TaskStatus,
    VerificationState,
)
from app.models import IncidentStatus as _S
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


# What to ask the agent for, per intervention. This was a single hardcoded
# "Refund payment X" string, which was correct while REFUND was the only
# executable intervention and silently wrong the moment PAYMENT_LINK joined it:
# a link candidate was dispatched as a refund request, which the policy engine
# then refused because a failed payment is not refundable. It failed safe and it
# failed for the wrong reason, and a recovery that never happens because the
# system asked the wrong question is still a recovery that never happens.
_REQUEST = {
    Intervention.REFUND: (
        "Refund payment {payment} amount {amount} as recovery for incident "
        "{incident} (candidate {candidate})."),
    Intervention.PAYMENT_LINK: (
        "Send a payment link for payment {payment} as recovery for incident "
        "{incident} (candidate {candidate})."),
}


def _request_for(plan: RecoveryPlan, candidate: RecoveryCandidate) -> str:
    template = _REQUEST.get(candidate.intervention)
    if template is None:
        raise RecoveryStopped(StopDecision(
            Disposition.STOP, "no_request_template",
            f"{candidate.intervention.value} has no dispatch form; it cannot be "
            f"asked for."))
    return template.format(payment=candidate.payment_id,
                           amount=candidate.amount_minor,
                           incident=plan.incident_id, candidate=candidate.id)


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

    # v2 §20. Dispatching a candidate IS the policy evaluation: the task below
    # goes through the same gates as any other financial action, and the
    # incident should say so while it happens rather than after.
    _advance(session, session.get(Incident, plan.incident_id), _S.POLICY_EVALUATING,
             reason="Recovery candidate dispatched for policy evaluation.")

    request = _request_for(plan, candidate)
    out = AgentRuntime(session, principal).run(request, incident_id=plan.incident_id)

    # Policy halted the run for a human. The incident follows the task, which is
    # this module's rule everywhere: task status in, incident status out.
    if out.task.status is TaskStatus.AWAITING_APPROVAL:
        _advance(session, out.task, _S.APPROVAL_REQUIRED,
                 reason="Policy requires human approval before this action.")

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
    VerificationState.FAILED: CandidateStatus.FAILED,
    VerificationState.PARTIAL: CandidateStatus.FAILED,
    VerificationState.UNKNOWN: CandidateStatus.UNKNOWN,
}


def _paid_share(cand: RecoveryCandidate, link) -> int:
    """What the provider reports paid on a link, in the candidate's attributed
    unit and never more than it. An unreadable link keeps the last share that
    was established: paid money does not become unpaid because a read failed."""
    if link is None:
        return cand.actual_recovery_minor or 0
    if link.amount_minor <= 0:
        return 0
    paid = max(0, min(link.amount_paid_minor, link.amount_minor))
    return cand.attributed_amount_minor * paid // link.amount_minor


def _settle_one(session, cand: RecoveryCandidate, action: AgentAction,
                link) -> tuple[CandidateStatus, int]:
    """What one verified action means for its candidate — MerchantOps §49.

    A verified SUCCESS does not mean the same thing for every intervention, and
    treating it as if it did is how a platform ends up reporting money it never
    recovered.

        refund        SUCCESS = the money went back        -> RECOVERED
        payment link  SUCCESS = a link now exists          -> ATTEMPTED
        notification  SUCCESS = a message was sent         -> ATTEMPTED

    A link is recovery only once somebody pays it, which is a fact about the
    LINK's state and not about our request to create one. Before this
    distinction existed, dispatching a payment link and verifying it reported
    the full payment amount as recovered while no customer had paid anything —
    exactly the claim §49 says the platform should never make.
    """
    state = action.verification_state
    if state is None:
        return cand.status, cand.actual_recovery_minor

    if action.action_type == "payment_link":
        # Money paid against a link is a fact about the LINK, and it holds
        # whatever the latest verification said about the link's usability. A
        # link that was part-paid and then expired used to settle to
        # (FAILED, 0), erasing money the customer had actually paid.
        paid_share = _paid_share(cand, link)
        # `link` is the provider's current read of it, fetched by `settle_plan`
        # before anything was written; None when it could not be read.
        if link is not None and link.status == "paid":
            return CandidateStatus.RECOVERED, cand.attributed_amount_minor
        if state is VerificationState.SUCCESS:
            # Sent and usable, perhaps part-paid. Not failed: the customer may
            # still pay, so the candidate stays ATTEMPTED.
            return CandidateStatus.ATTEMPTED, paid_share
        # Expired, cancelled or unreadable: settled as verification says, and
        # what was paid stays paid.
        return _FROM_VERIFICATION.get(state, CandidateStatus.UNKNOWN), paid_share

    if state is not VerificationState.SUCCESS:
        return _FROM_VERIFICATION.get(state, CandidateStatus.UNKNOWN), 0

    if action.action_type == "refund":
        return CandidateStatus.RECOVERED, action.amount_minor

    # A message was delivered. Nothing has been recovered by delivering it.
    return CandidateStatus.ATTEMPTED, 0


def settle_plan(session, plan: RecoveryPlan, adapter=None) -> dict:
    """Read each dispatched candidate's outcome back from its action.

    The candidate does not decide what happened; the verified action does, and
    what a verified action MEANS depends on what was done. MerchantOps §49 keeps
    expected and actual apart, and an UNKNOWN action has not been shown to have
    moved anything.
    """
    from app.db import checkpoint
    from app.integrations.razorpay.adapter import get_adapter

    adapter = adapter or get_adapter(session)
    counts = {s.value: 0 for s in CandidateStatus}
    recovered = 0

    # Read, then call out, then write -- in that order (app/boundaries.py).
    # This loop used to write each candidate and then ask the provider about
    # the next one's link, holding every row it had settled so far locked
    # across a network call.
    pairs = []
    for cand in session.query(RecoveryCandidate).filter(
            RecoveryCandidate.plan_id == plan.id).all():
        action = None
        if cand.task_id:
            action = (session.query(AgentAction)
                      .filter(AgentAction.task_id == cand.task_id)
                      .order_by(AgentAction.created_at.desc()).first())
        pairs.append((cand, action))

    # Every link with a reference, whatever its verification says: a link
    # that has since expired may still carry money that was paid on it.
    to_read = [a for _, a in pairs
               if a is not None and a.action_type == "payment_link"
               and a.verification_state is not None
               and a.external_reference]
    links: dict[str, object] = {}
    if to_read:
        checkpoint(session)
        for action in to_read:
            try:
                links[action.id] = adapter.get_payment_link(action.external_reference)
            except Exception:
                links[action.id] = None

    for cand, action in pairs:
        if action is not None:
            action.recovery_candidate_id = cand.id
            cand.status, cand.actual_recovery_minor = _settle_one(
                session, cand, action, links.get(action.id))
        counts[cand.status.value] += 1
        recovered += cand.actual_recovery_minor

    outstanding = session.query(RecoveryCandidate).filter(
        RecoveryCandidate.plan_id == plan.id,
        RecoveryCandidate.status.in_([CandidateStatus.ELIGIBLE,
                                      CandidateStatus.ATTEMPTED])).count()
    if outstanding == 0 and plan.status in (PlanStatus.DRAFT, PlanStatus.ACTIVE):
        plan.status = PlanStatus.COMPLETED
        from app.events.bus import publish
        publish(session, "recovery.completed", merchant_id=plan.merchant_id,
                incident_id=plan.incident_id, entity_id=plan.id,
                payload={"plan_id": plan.id, "by_status": counts,
                         "actual_recovery_minor": recovered,
                         "expected_recovery_minor": plan.expected_recovery_minor})
    session.flush()

    return {"plan_id": plan.id, "status": plan.status.value,
            "by_status": counts, "actual_recovery_minor": recovered,
            "expected_recovery_minor": plan.expected_recovery_minor}
