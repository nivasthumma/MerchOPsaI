"""Measured recovery outcomes — MerchantOps v2 §40.

The assertions that matter are the two that stop §40 becoming a hazard:
history may not widen the set of interventions the planner considers safe, and
an intervention nobody has attempted must not read as one that never works.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.detection import detect
from app.models import CandidateStatus, Incident, IncidentType, Intervention
from app.recovery import plan_recovery
from app.recovery.history import (
    MIN_SAMPLE,
    outcome_for,
    outcomes,
    rank,
)


@pytest.fixture
def plan(db, owner):
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())
    return plan_recovery(db, inc, principal=owner).plan


def _settle(db, plan, *, recovered: int, failed: int, amount: int = 10_000):
    """Give an intervention a settled track record to measure."""
    from app.models import RecoveryCandidate

    rows = (db.query(RecoveryCandidate)
            .filter_by(plan_id=plan.id)
            .order_by(RecoveryCandidate.rank).limit(recovered + failed).all())
    assert len(rows) == recovered + failed, "not enough candidates to settle"
    for i, c in enumerate(rows):
        c.attributed_amount_minor = amount
        if i < recovered:
            c.status = CandidateStatus.RECOVERED
            c.actual_recovery_minor = amount
        else:
            c.status = CandidateStatus.FAILED
            c.actual_recovery_minor = 0
    db.flush()


# ------------------------------------------------- nothing is not zero percent
def test_an_unattempted_intervention_is_unmeasured_not_zero(db):
    """The trap §40 sets for a naive implementation.

    Reporting "never attempted" as 0% makes an intervention look thoroughly
    tested and useless, and suppresses it permanently: it never gets tried, so
    it never accumulates history, so it stays at 0%.
    """
    out = outcome_for(db, "MERCH_A", Intervention.RETRY)
    assert out.measured is False
    assert out.attempts == 0
    # None, not 0.0. A caller conflating the two gets a TypeError, not a
    # quietly pessimistic number.
    assert out.rate is None
    assert out.value_rate is None


def test_a_thin_record_is_not_a_rate(db, plan):
    """Three successes out of three is three successes, not 100%."""
    _settle(db, plan, recovered=3, failed=0)
    out = outcome_for(db, "MERCH_A", plan.intervention)
    assert out.attempts == 3
    assert out.measured is False
    assert out.rate is None


def test_a_rate_appears_once_there_is_enough_of_a_record(db, plan):
    _settle(db, plan, recovered=6, failed=MIN_SAMPLE - 6)
    out = outcome_for(db, "MERCH_A", plan.intervention)
    assert out.attempts == MIN_SAMPLE
    assert out.measured is True
    assert out.rate == pytest.approx(6 / MIN_SAMPLE)


# -------------------------------------------------------- count vs value rate
def test_the_value_rate_is_reported_separately_from_the_count_rate(db, plan):
    """Nine small recoveries and one large loss is 90% by count and much less
    by value. The planner estimates money, so the two cannot be one number."""
    from app.models import RecoveryCandidate

    rows = (db.query(RecoveryCandidate).filter_by(plan_id=plan.id)
            .order_by(RecoveryCandidate.rank).limit(10).all())
    assert len(rows) == 10
    for i, c in enumerate(rows):
        if i < 9:
            c.status = CandidateStatus.RECOVERED
            c.attributed_amount_minor = 100_00
            c.actual_recovery_minor = 100_00
        else:
            c.status = CandidateStatus.FAILED
            c.attributed_amount_minor = 10_000_00
            c.actual_recovery_minor = 0
    db.flush()

    out = outcome_for(db, "MERCH_A", plan.intervention)
    assert out.rate == pytest.approx(0.9)
    assert out.value_rate == pytest.approx(900_00 / 10_900_00)
    assert out.value_rate < out.rate


# ------------------------------------------------------------ what counts
def test_an_attempt_still_in_flight_does_not_depress_the_rate(db, plan):
    """ATTEMPTED is unsettled. Counting it as a non-recovery would make every
    rate a function of how much work happens to be running."""
    from app.models import RecoveryCandidate

    _settle(db, plan, recovered=MIN_SAMPLE, failed=0)
    settled = outcome_for(db, "MERCH_A", plan.intervention)
    assert settled.rate == pytest.approx(1.0)

    in_flight = (db.query(RecoveryCandidate)
                 .filter_by(plan_id=plan.id, status=CandidateStatus.ELIGIBLE)
                 .first())
    in_flight.status = CandidateStatus.ATTEMPTED
    db.flush()

    assert outcome_for(db, "MERCH_A", plan.intervention).rate == pytest.approx(1.0)


def test_an_unknown_external_state_counts_against_the_rate(db, plan):
    """§53. An unresolved outcome is not a success, and a recovery rate that
    treated it as one would report money back that nobody has established."""
    from app.models import RecoveryCandidate

    _settle(db, plan, recovered=MIN_SAMPLE, failed=0)
    unknown = (db.query(RecoveryCandidate)
               .filter_by(plan_id=plan.id, status=CandidateStatus.ELIGIBLE)
               .first())
    unknown.status = CandidateStatus.UNKNOWN
    unknown.attributed_amount_minor = 10_000
    db.flush()

    out = outcome_for(db, "MERCH_A", plan.intervention)
    assert out.attempts == MIN_SAMPLE + 1
    assert out.recovered == MIN_SAMPLE
    assert out.rate < 1.0


# ------------------------------------------------------------------ tenancy
def test_one_merchants_record_says_nothing_about_another(db, plan):
    """Pooling would be a cross-tenant read wearing a statistic (§54)."""
    _settle(db, plan, recovered=MIN_SAMPLE, failed=0)
    assert outcome_for(db, "MERCH_A", plan.intervention).measured is True
    assert outcome_for(db, "MERCH_B", plan.intervention).measured is False
    assert outcomes(db, "MERCH_B") == {}


# ------------------------------------------------- the constraint §40 needs
def test_ranking_cannot_introduce_an_intervention_the_planner_refused(db, plan):
    """v2 §40's "strategy execution remains subject to deterministic policy",
    expressed as a signature.

    The planner maps a degraded method to PAYMENT_LINK and not RETRY, because
    re-presenting a customer to the failing rail is the same failure again. If
    history could add RETRY back, a good historical rate would send customers
    to a broken rail because it used to work.
    """
    # Give RETRY a perfect record. It is still not on the permitted list.
    db.execute(text("""
        UPDATE recovery_candidates SET intervention = 'RETRY',
               status = 'RECOVERED', actual_recovery_minor = attributed_amount_minor
        WHERE plan_id = :p
    """), {"p": plan.id})
    db.flush()
    assert outcome_for(db, "MERCH_A", Intervention.RETRY).rate == pytest.approx(1.0)

    permitted = [Intervention.PAYMENT_LINK]
    ranked = rank(db, "MERCH_A", permitted)
    assert [o.intervention for o in ranked] == permitted
    assert Intervention.RETRY not in {o.intervention for o in ranked}


def test_ranking_puts_the_best_measured_option_first(db, plan):
    permitted = [Intervention.PAYMENT_LINK, Intervention.REFUND]
    _settle(db, plan, recovered=MIN_SAMPLE, failed=0)

    ranked = rank(db, "MERCH_A", permitted)
    assert ranked[0].intervention is plan.intervention
    assert ranked[0].measured is True


def test_an_unmeasured_option_cannot_displace_one_with_evidence(db, plan):
    """It is not ranked below because it is worse. Nothing is known about it."""
    _settle(db, plan, recovered=MIN_SAMPLE // 2, failed=MIN_SAMPLE // 2)
    ranked = rank(db, "MERCH_A", [Intervention.REFUND, Intervention.PAYMENT_LINK])
    assert ranked[0].measured is True
    assert ranked[-1].measured is False


def test_ranking_is_stable_when_nothing_is_measured(db):
    """With no evidence at all, the planner's own order survives untouched."""
    permitted = [Intervention.PAYMENT_LINK, Intervention.REFUND,
                 Intervention.HUMAN_ESCALATION]
    assert [o.intervention for o in rank(db, "MERCH_A", permitted)] == permitted


# ------------------------------------------------ the planner actually uses it
def test_the_estimate_says_it_is_a_proxy_while_there_is_no_record(db, plan):
    """A reader comparing two plans has to know which figure came from
    evidence and which from an assumption."""
    assert "prior-period success rate" in plan.expected_recovery_basis
    assert "No measured recovery rate yet" in plan.expected_recovery_basis
    assert str(MIN_SAMPLE) in plan.expected_recovery_basis


def test_the_estimate_switches_to_the_measured_rate_once_there_is_one(db, owner):
    """v2 §40: "the recovery planner can consider these outcomes".

    The prior-period success rate assumes re-presentation converts as well as
    the rail did before it broke. A settled record measures the same question
    directly, so it replaces the assumption — and the basis says which is in
    use, because the two are not interchangeable.
    """
    from app.models import RecoveryCandidate
    from app.recovery.planner import compute_plan

    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())
    first = plan_recovery(db, inc, principal=owner).plan
    proxy_expected = first.expected_recovery_minor
    assert "prior-period" in first.expected_recovery_basis

    # A settled record: half the value recovered, over enough attempts to count.
    rows = (db.query(RecoveryCandidate).filter_by(plan_id=first.id)
            .order_by(RecoveryCandidate.rank).limit(MIN_SAMPLE).all())
    assert len(rows) == MIN_SAMPLE
    for i, c in enumerate(rows):
        c.attributed_amount_minor = 10_000
        c.status = (CandidateStatus.RECOVERED if i < MIN_SAMPLE // 2
                    else CandidateStatus.FAILED)
        c.actual_recovery_minor = 10_000 if i < MIN_SAMPLE // 2 else 0
    db.flush()

    draft = compute_plan(db, inc)
    assert "MEASURED recovery rate" in draft.basis
    assert f"{MIN_SAMPLE} settled attempts" in draft.basis
    # Measured at ~50% against a rail that used to convert at ~92%, so the
    # estimate falls. The point is not the direction — it is that the figure
    # now moves with outcomes instead of with an assumption.
    assert draft.expected_recovery_minor != proxy_expected


def test_the_measured_rate_is_by_value_not_by_count(db, plan, owner):
    """The planner multiplies money, so a good count rate on small recoveries
    must not inflate what a campaign is estimated to be worth."""
    from app.models import RecoveryCandidate
    from app.recovery.planner import compute_plan

    rows = (db.query(RecoveryCandidate).filter_by(plan_id=plan.id)
            .order_by(RecoveryCandidate.rank).limit(MIN_SAMPLE).all())
    for i, c in enumerate(rows):
        if i < MIN_SAMPLE - 1:
            c.status = CandidateStatus.RECOVERED
            c.attributed_amount_minor = 100_00
            c.actual_recovery_minor = 100_00
        else:
            c.status = CandidateStatus.FAILED
            c.attributed_amount_minor = 10_000_00
            c.actual_recovery_minor = 0
    db.flush()

    out = outcome_for(db, "MERCH_A", plan.intervention)
    assert out.rate > 0.85            # nine of ten, by count
    assert out.value_rate < 0.15      # a fraction of the money

    inc = db.get(Incident, plan.incident_id)
    draft = compute_plan(db, inc)
    # The basis quotes the value rate, which is the conservative one.
    assert f"{out.value_rate:.1%} by value" in draft.basis


def test_every_outcome_reports_what_it_was_computed_from(db, plan):
    _settle(db, plan, recovered=MIN_SAMPLE, failed=0)
    d = outcome_for(db, "MERCH_A", plan.intervention).as_dict()
    for key in ("intervention", "attempts", "recovered", "attempted_minor",
                "recovered_minor", "measured", "rate", "value_rate",
                "min_sample"):
        assert key in d, key
    assert d["min_sample"] == MIN_SAMPLE
