"""Revenue-recovery measurement — MerchantOps §49, §50, §51.

§49 ends with a sentence that is the whole test file:

> The platform should never call the entire INR 4.72L "recovered."
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.detection import detect
from app.models import (
    CandidateStatus, Incident, IncidentType, Intervention, PaymentLink,
    VerificationState,
)
from app.recovery import plan_recovery
from app.recovery.dispatch import dispatch_candidate, executable_candidates, settle_plan
from app.recovery.ledger import build_ledger, dashboard


def _plan_for(db, kind: IncidentType):
    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(Incident.incident_type == kind).first()
    assert inc is not None
    return plan_recovery(db, inc)


def _single_candidate(db, plan):
    """Shrink a campaign to one action so it is not bulk and can be dispatched."""
    rest = executable_candidates(db, plan)[1:]
    for c in rest:
        c.executable = False
    db.flush()
    return executable_candidates(db, plan)[0]


# ------------------------------------------------------------ the invariants
def test_the_figures_nest(db):
    for kind in (IncidentType.PAYMENT_DEGRADATION, IncidentType.DUPLICATE_PAYMENT):
        _plan_for(db, kind)
    led = build_ledger(db, "MERCH_A")
    assert led.invariants() == [], led.as_dict()
    assert led.at_risk_minor >= led.recoverable_minor >= led.attempted_minor
    assert led.settled_minor <= led.attempted_minor


def test_nothing_is_recovered_before_anything_is_attempted(db):
    for kind in (IncidentType.PAYMENT_DEGRADATION, IncidentType.DUPLICATE_PAYMENT):
        _plan_for(db, kind)
    led = build_ledger(db, "MERCH_A")
    assert led.attempted_minor == 0
    assert led.recovered_minor == 0
    assert led.failed_minor == 0
    assert led.unknown_minor == 0
    # Planning alone must never move a recovery number.
    assert led.recoverable_minor > 0


def test_candidate_shares_sum_to_the_plans_own_figure(db):
    """The parts define the whole. Rounding each share independently drifts, and
    a total larger than the figure it is a share of breaks the ordering."""
    r = _plan_for(db, IncidentType.PAYMENT_DEGRADATION)
    parts = sum(c.attributed_amount_minor for c in r.candidates)
    assert parts == r.plan.eligible_recovery_minor
    assert parts <= r.plan.revenue_at_risk_minor


# ---------------------------------------------- what "recovered" is allowed to mean
def test_a_refund_that_verified_is_recovered(db, owner):
    r = _plan_for(db, IncidentType.DUPLICATE_PAYMENT)
    cand = _single_candidate(db, r.plan)
    out = dispatch_candidate(db, r.plan, cand, owner)
    res = approve_and_execute(db, out["task"].id, owner)
    assert res["action"].verification_state is VerificationState.SUCCESS

    settle_plan(db, r.plan)
    db.refresh(cand)
    assert cand.status is CandidateStatus.RECOVERED
    assert cand.actual_recovery_minor == res["action"].amount_minor


def test_a_payment_link_that_was_merely_sent_is_not_recovered(db, owner):
    """The defect this phase found. A verified payment link means a link now
    exists — no customer has paid anything. Reporting the full charge as
    recovered is exactly the claim §49 forbids."""
    r = _plan_for(db, IncidentType.PAYMENT_DEGRADATION)
    assert r.plan.intervention is Intervention.PAYMENT_LINK
    cand = _single_candidate(db, r.plan)
    out = dispatch_candidate(db, r.plan, cand, owner)
    res = approve_and_execute(db, out["task"].id, owner)
    assert res["action"].action_type == "payment_link"
    assert res["action"].verification_state is VerificationState.SUCCESS

    settle_plan(db, r.plan)
    db.refresh(cand)
    assert cand.status is CandidateStatus.ATTEMPTED
    assert cand.actual_recovery_minor == 0

    led = build_ledger(db, "MERCH_A")
    assert led.attempted_minor > 0
    assert led.recovered_minor == 0
    assert led.outstanding_minor > 0


def test_a_payment_link_becomes_recovery_only_once_it_is_paid(db, owner):
    r = _plan_for(db, IncidentType.PAYMENT_DEGRADATION)
    cand = _single_candidate(db, r.plan)
    out = dispatch_candidate(db, r.plan, cand, owner)
    res = approve_and_execute(db, out["task"].id, owner)
    settle_plan(db, r.plan)
    db.refresh(cand)
    assert cand.actual_recovery_minor == 0

    # The customer pays.
    db.execute(text("UPDATE payment_links SET status = 'paid' WHERE id = :i"),
               {"i": res["action"].external_reference})
    db.flush()
    settle_plan(db, r.plan)
    db.refresh(cand)
    assert cand.status is CandidateStatus.RECOVERED
    # Counted at its ATTRIBUTED share, not the gross charge: only that part was
    # ever at risk from this incident.
    assert cand.actual_recovery_minor == cand.attributed_amount_minor
    assert cand.actual_recovery_minor <= cand.amount_minor


def test_the_dispatcher_asks_for_the_intervention_that_was_planned(db, owner):
    """A single hardcoded "Refund payment X" was correct while REFUND was the
    only executable intervention and wrong the moment PAYMENT_LINK joined it —
    a link candidate was dispatched as a refund and refused for being a failed
    payment. Safe, and for the wrong reason."""
    r = _plan_for(db, IncidentType.PAYMENT_DEGRADATION)
    cand = _single_candidate(db, r.plan)
    out = dispatch_candidate(db, r.plan, cand, owner)
    tools = [tc.tool_name for tc in out["task"].tool_calls]
    assert "generate_payment_link" in tools
    assert "request_refund" not in tools


def test_an_unknown_action_lands_in_the_unknown_bucket(db, owner):
    """Not folded into either neighbour. §33 exists to keep the size of what we
    do not know visible."""
    from app.integrations.razorpay.faults import Fault, FaultInjector

    r = _plan_for(db, IncidentType.DUPLICATE_PAYMENT)
    cand = _single_candidate(db, r.plan)
    out = dispatch_candidate(db, r.plan, cand, owner)
    approve_and_execute(db, out["task"].id, owner,
                        injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    settle_plan(db, r.plan)
    db.refresh(cand)
    assert cand.status is CandidateStatus.UNKNOWN

    led = build_ledger(db, "MERCH_A")
    assert led.unknown_minor > 0
    assert led.recovered_minor == 0
    assert led.failed_minor == 0
    assert led.invariants() == []


# ------------------------------------------------------------------ §50, §51
def test_the_ledger_is_merchant_scoped(db):
    for kind in (IncidentType.PAYMENT_DEGRADATION, IncidentType.DUPLICATE_PAYMENT):
        _plan_for(db, kind)
    assert build_ledger(db, "MERCH_A").at_risk_minor > 0
    assert build_ledger(db, "MERCH_B").at_risk_minor == 0
    assert build_ledger(db, "MERCH_B").recoverable_minor == 0


def test_a_resolved_incident_leaves_the_at_risk_figure(db):
    """Otherwise at-risk grows monotonically forever and stops meaning anything."""
    from app.incidents.lifecycle import transition
    from app.models import IncidentStatus

    _plan_for(db, IncidentType.DUPLICATE_PAYMENT)
    before = build_ledger(db, "MERCH_A").at_risk_minor
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.DUPLICATE_PAYMENT).first()
    transition(db, inc, IncidentStatus.INVESTIGATING, reason="t")
    transition(db, inc, IncidentStatus.RESOLVED, reason="t")
    after = build_ledger(db, "MERCH_A").at_risk_minor
    assert after < before


def test_the_dashboard_reports_incidents_and_agent_activity(db, owner):
    from app.agent.runtime import AgentRuntime

    _plan_for(db, IncidentType.DUPLICATE_PAYMENT)
    AgentRuntime(db, owner).run("Why did revenue drop this week?")
    d = dashboard(db, "MERCH_A")

    assert d["recovery"]["invariants_broken"] == []
    assert d["incidents"]["open"] > 0
    assert d["agent_activity"]["investigations"] >= 1
    assert d["agent_activity"]["tool_calls"] > 0
    assert d["agent_activity"]["recommendations"] >= 1


def test_the_ledger_publishes_its_basis(db):
    """A figure whose unit is not stated is a figure a reader will assume the
    wrong thing about."""
    _plan_for(db, IncidentType.DUPLICATE_PAYMENT)
    d = build_ledger(db, "MERCH_A").as_dict()
    assert "attributed" in d["basis"].lower()
    assert "paid" in d["basis"].lower()


def test_the_incident_page_carries_its_recovery_and_timeline(db, owner):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app
    from app.incidents.manager import investigate

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.DUPLICATE_PAYMENT).first()
    investigate(db, inc, owner)
    plan_recovery(db, inc)
    db.commit()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        view = c.get(f"/incidents/{inc.id}",
                     headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}).json()
    sec.reset_rate_limits()

    assert view["recovery"] is not None
    assert view["recovery"]["intervention"]
    assert view["evidence"]
    assert view["timeline"]
    events = [e["event"] for e in view["timeline"]]
    assert events[0] == "incident_detected"
    assert "recovery_planned" in events
