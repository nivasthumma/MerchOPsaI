"""Recovery planning, budgets and stopping rules — MerchantOps §22, §23, §27, §28."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.detection import detect
from app.models import (
    CandidateStatus, Incident, IncidentType, Intervention, PlanStatus,
    RecoveryCandidate, RecoveryPlan,
)
from app.recovery import plan_recovery
from app.recovery.dispatch import (
    RecoveryStopped, assess_candidate_risk, dispatch_candidate,
    executable_candidates, settle_plan,
)
from app.recovery.stopping import Disposition, check_budget, evaluate_stopping_rules


def _incident(db, kind: IncidentType) -> Incident:
    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(Incident.incident_type == kind).first()
    assert inc is not None, f"no {kind.value} incident was detected"
    return inc


# ------------------------------------------------------------------ §23 plan
def test_degradation_plans_a_payment_link_not_a_retry(db):
    """Re-presenting a customer to the rail that is currently failing is the
    same failure again, not a recovery."""
    inc = _incident(db, IncidentType.PAYMENT_DEGRADATION)
    r = plan_recovery(db, inc)
    assert r.plan.intervention is Intervention.PAYMENT_LINK
    assert r.candidates


def test_duplicate_plans_a_refund_of_the_excess_only(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    assert r.plan.intervention is Intervention.REFUND
    # The earliest capture is the real payment; refunding it would undo the sale.
    assert r.plan.incident_id == inc.id
    originals = {inc.signals["first_payment_id"]}
    assert not {c.payment_id for c in r.candidates} & originals


def test_planning_is_idempotent(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    first = plan_recovery(db, inc)
    second = plan_recovery(db, inc)
    assert first.created and not second.created
    assert first.plan.id == second.plan.id
    assert db.query(RecoveryPlan).filter(RecoveryPlan.incident_id == inc.id).count() == 1


def test_planning_executes_nothing(db):
    """§23 ends at candidates. Planning must not touch money."""
    from app.models import AgentAction, Refund
    before = (db.query(Refund).count(), db.query(AgentAction).count())
    for inc in db.query(Incident).all():
        plan_recovery(db, inc)
    assert (db.query(Refund).count(), db.query(AgentAction).count()) == before


# -------------------------------------------------------------- §22 numbers
def test_the_49_ordering_holds(db):
    """revenue at risk >= eligible >= expected. An eligible figure above the
    at-risk figure claims the merchant can recover more than the incident cost."""
    for inc in db.query(Incident).all():
        p = plan_recovery(db, inc).plan
        assert p.revenue_at_risk_minor >= p.eligible_recovery_minor, p.id
        assert p.eligible_recovery_minor >= p.expected_recovery_minor, p.id


def test_expected_recovery_carries_its_basis(db):
    inc = _incident(db, IncidentType.PAYMENT_DEGRADATION)
    p = plan_recovery(db, inc).plan
    assert p.expected_recovery_basis
    assert "estimate" in p.expected_recovery_basis.lower()
    assert p.expected_recovery_minor > 0


def test_actual_recovery_starts_at_zero_and_is_never_the_expected_figure(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    assert all(c.actual_recovery_minor == 0 for c in r.candidates)
    assert any(c.expected_recovery_minor > 0 for c in r.candidates)


# ---------------------------------------------------------- §28 eligibility
def test_an_opted_out_customer_is_not_contacted(db):
    inc = _incident(db, IncidentType.PAYMENT_DEGRADATION)
    r = plan_recovery(db, inc)
    opted = [c for c in r.candidates if c.ineligible_reason == "customer_opted_out"]
    assert opted, "the opt-out rule was never exercised by the seeded dataset"
    assert all(c.status is CandidateStatus.INELIGIBLE for c in opted)
    assert all(c.expected_recovery_minor == 0 for c in opted)


def test_an_opted_out_customer_is_still_refunded(db):
    """Opt-out governs contact, not debt. Money owed back reaches the customer
    through the payment rail, not through marketing consent."""
    from app.recovery.planner import _eligibility

    ok, why = _eligibility({"amount_minor": 5000, "contact_opted_out": True},
                           Intervention.REFUND)
    assert ok, why
    ok, why = _eligibility({"amount_minor": 5000, "contact_opted_out": True},
                           Intervention.PAYMENT_LINK)
    assert not ok and why == "customer_opted_out"


# ------------------------------------------------------------- §27 budget
def test_budget_is_copied_onto_the_plan(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    p = plan_recovery(db, inc).plan
    assert (p.max_recovery_minor, p.max_actions, p.max_attempts_per_customer,
            p.max_duration_seconds) == (50_000_00, 500, 2, 86_400)


def test_a_merchant_raising_their_limit_does_not_widen_a_live_plan(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    p = plan_recovery(db, inc).plan
    original = p.max_recovery_minor
    from app.config import get_settings
    get_settings().recovery_max_amount_minor = 999_999_00
    try:
        db.refresh(p)
        assert p.max_recovery_minor == original
    finally:
        get_settings().recovery_max_amount_minor = 50_000_00


def test_budget_refuses_a_spend_over_the_maximum(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    plan, cand = r.plan, r.candidates[0]
    plan.max_recovery_minor = cand.amount_minor - 1
    db.flush()
    d = check_budget(db, plan, cand)
    assert d.disposition is Disposition.STOP
    assert d.rule == "recovery_budget_exhausted"


def test_budget_refuses_once_the_action_count_is_reached(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    plan, cand = r.plan, r.candidates[0]
    plan.max_actions = 0
    db.flush()
    assert check_budget(db, plan, cand).rule == "max_actions_reached"


def test_budget_refuses_a_customer_already_approached(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    plan, cand = r.plan, r.candidates[0]
    cand.attempts = plan.max_attempts_per_customer
    db.flush()
    assert check_budget(db, plan, cand).rule == "max_attempts_per_customer"


def test_an_expired_plan_stops(db):
    from datetime import datetime, timedelta, timezone
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    r.plan.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    assert check_budget(db, r.plan).rule == "max_duration_exceeded"


# ---------------------------------------------------------- §28 stopping
def test_provider_unavailable_escalates_rather_than_stopping(db):
    """The obstacle is outside the campaign, so it is not the campaign's
    decision to make."""
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    d = evaluate_stopping_rules(db, r.plan, r.candidates[0], provider_available=False)
    assert d.disposition is Disposition.ESCALATE
    assert d.rule == "provider_unavailable"


def test_risk_above_the_ceiling_escalates(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    d = evaluate_stopping_rules(db, r.plan, r.candidates[0], risk_level="CRITICAL")
    assert d.disposition is Disposition.ESCALATE
    assert d.rule == "risk_exceeds_allowed_level"
    # HIGH is the ceiling, not above it.
    assert evaluate_stopping_rules(
        db, r.plan, r.candidates[0], risk_level="HIGH").disposition is Disposition.CONTINUE


def test_expected_recovery_below_threshold_stops(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    db.execute(text("UPDATE recovery_candidates SET expected_recovery_minor = 1 "
                    "WHERE plan_id = :p"), {"p": r.plan.id})
    db.flush()
    d = evaluate_stopping_rules(db, r.plan)
    assert d.disposition is Disposition.STOP
    assert d.rule == "expected_recovery_below_threshold"


def test_a_plan_with_no_candidates_escalates_as_insufficient_evidence(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    db.execute(text("DELETE FROM recovery_candidates WHERE plan_id = :p"),
               {"p": r.plan.id})
    db.flush()
    d = evaluate_stopping_rules(db, r.plan)
    assert d.disposition is Disposition.ESCALATE
    assert d.rule == "evidence_insufficient"


def test_a_stop_is_applied_not_merely_logged(db, owner):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    r.plan.max_actions = 0
    db.flush()
    with pytest.raises(RecoveryStopped) as e:
        dispatch_candidate(db, r.plan, r.candidates[0], owner)
    assert e.value.decision.rule == "max_actions_reached"
    db.refresh(r.plan)
    assert r.plan.status is PlanStatus.STOPPED
    assert r.plan.stop_rule == "max_actions_reached"


def test_a_stopped_plan_stays_stopped(db, owner):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    r.plan.max_actions = 0
    db.flush()
    with pytest.raises(RecoveryStopped):
        dispatch_candidate(db, r.plan, r.candidates[0], owner)
    r.plan.max_actions = 500          # the bound that fired is lifted
    db.flush()
    with pytest.raises(RecoveryStopped) as e:
        dispatch_candidate(db, r.plan, r.candidates[0], owner)
    assert e.value.decision.rule == "max_actions_reached"   # still refused


# ------------------------------------------------------------ §24 bulk risk
def test_a_single_candidate_plan_is_not_bulk(db):
    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    assert len(executable_candidates(db, r.plan)) == 1
    risk = assess_candidate_risk(db, r.plan, r.candidates[0])
    assert risk.level == "HIGH"
    assert not any(f.name == "bulk_size" for f in risk.factors)


def test_a_multi_candidate_refund_plan_is_critical_and_escalates(db, owner):
    """§24: bulk refund -> CRITICAL. Grading each action as if it stood alone
    is how a bulk campaign escapes the class the spec assigns it."""
    row = db.execute(text("""
        SELECT order_id, customer_id, amount_minor, method, created_at
        FROM payments WHERE id = 'SYN_PAY_0002'
    """)).mappings().one()
    db.execute(text("""
        INSERT INTO payments (id, merchant_id, order_id, customer_id, amount_minor,
                              currency, method, status, amount_refunded_minor,
                              created_at, external_provider, external_payment_id)
        VALUES ('SYN_PAY_TRIPLE', 'MERCH_A', :o, :c, :a, 'INR', :m, 'captured', 0,
                :t + interval '60 seconds', 'razorpay', 'pay_MOCKTRIPLE')
    """), {"o": row["order_id"], "c": row["customer_id"], "a": row["amount_minor"],
           "m": row["method"], "t": row["created_at"]})
    db.flush()

    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    assert len(executable_candidates(db, r.plan)) == 2

    risk = assess_candidate_risk(db, r.plan, r.candidates[0])
    assert risk.level == "CRITICAL"
    assert any(f.name == "bulk_size" for f in risk.factors)

    with pytest.raises(RecoveryStopped) as e:
        dispatch_candidate(db, r.plan, r.candidates[0], owner)
    assert e.value.decision.rule == "risk_exceeds_allowed_level"
    db.refresh(r.plan)
    assert r.plan.status is PlanStatus.ESCALATED


# ------------------------------------------------------------- dispatch
def test_a_non_executable_candidate_is_refused(db, owner):
    """A PAYMENT_LINK candidate is a real recommendation with no tool behind it.
    It must never be counted as actionable."""
    inc = _incident(db, IncidentType.PAYMENT_DEGRADATION)
    r = plan_recovery(db, inc)
    assert not any(c.executable for c in r.candidates)
    with pytest.raises(RecoveryStopped) as e:
        dispatch_candidate(db, r.plan, r.candidates[0], owner)
    assert e.value.decision.rule == "not_executable"


def test_dispatch_goes_through_the_ordinary_approval_path(db, owner):
    from app.models import TaskStatus

    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    out = dispatch_candidate(db, r.plan, r.candidates[0], owner)

    # Nothing executed: the refund is HIGH risk and waits for a human, exactly
    # as it would if a person had asked for it directly.
    assert out["task"].status is TaskStatus.AWAITING_APPROVAL
    db.refresh(r.candidates[0])
    assert r.candidates[0].status is CandidateStatus.ATTEMPTED
    assert r.candidates[0].attempts == 1
    assert r.candidates[0].task_id == out["task"].id
    db.refresh(r.plan)
    assert r.plan.status is PlanStatus.ACTIVE


def test_settlement_reads_the_outcome_from_the_verified_action(db, owner):
    from app.agent.approval import approve_and_execute
    from app.models import VerificationState

    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    out = dispatch_candidate(db, r.plan, r.candidates[0], owner)
    res = approve_and_execute(db, out["task"].id, owner)
    assert res["action"].verification_state is VerificationState.SUCCESS

    report = settle_plan(db, r.plan)
    db.refresh(r.candidates[0])
    assert r.candidates[0].status is CandidateStatus.RECOVERED
    assert r.candidates[0].actual_recovery_minor == res["action"].amount_minor
    assert report["actual_recovery_minor"] == res["action"].amount_minor
    assert res["action"].recovery_candidate_id == r.candidates[0].id


def test_an_unknown_action_recovers_nothing(db, owner):
    """§49: an UNKNOWN action has not been shown to have moved anything, so it
    contributes zero to actual recovery — not its amount, and not a guess."""
    from app.agent.approval import approve_and_execute
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.models import VerificationState

    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    out = dispatch_candidate(db, r.plan, r.candidates[0], owner)
    res = approve_and_execute(db, out["task"].id, owner,
                              injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    assert res["action"].verification_state is VerificationState.UNKNOWN

    report = settle_plan(db, r.plan)
    db.refresh(r.candidates[0])
    assert r.candidates[0].status is CandidateStatus.UNKNOWN
    assert r.candidates[0].actual_recovery_minor == 0
    assert report["actual_recovery_minor"] == 0


# --------------------------------------------------- the stop must survive HTTP
def test_a_refused_dispatch_leaves_the_plan_stopped_over_http(db, owner):
    """Found by running the demo, not by the unit tests above — they call
    `dispatch_candidate` directly and never cross the session boundary.

    Raising an HTTPException unwinds `session_scope`, which rolls back. The
    record that the campaign stopped is exactly what gets discarded, leaving the
    plan DRAFT and the exhausted bound free to be tried again.
    """
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    inc = _incident(db, IncidentType.DUPLICATE_PAYMENT)
    r = plan_recovery(db, inc)
    r.plan.max_recovery_minor = 1
    cand_id = [c for c in r.candidates if c.executable][0].id
    plan_id = r.plan.id
    db.commit()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}
        resp = c.post(f"/recovery/candidates/{cand_id}/dispatch", headers=h)
        assert resp.status_code == 409
        assert resp.json()["detail"]["stop"]["rule"] == "recovery_budget_exhausted"

        after = c.get(f"/recovery/plans/{plan_id}", headers=h).json()
        assert after["status"] == "STOPPED", "the stop did not survive the response"
        assert after["stop_rule"] == "recovery_budget_exhausted"
    sec.reset_rate_limits()
