"""The campaign view of a plan — MerchantOps v2 §37, §38.

There is no campaign table and these tests are partly here to keep it that way:
`RecoveryPlan` already carries the objective, the figures, all five bounds and a
status. What was missing was budget *consumption* and the affected/eligible
split as figures, and those are computed rather than stored, so the assertions
are about them staying true as candidates move.
"""
from __future__ import annotations

import pytest

from app.detection import detect
from app.models import (
    CandidateStatus,
    Incident,
    IncidentType,
    PlanStatus,
    RecoveryCandidate,
)
from app.recovery import plan_recovery
from app.recovery.campaign import active_campaigns, summary


@pytest.fixture
def plan(db, owner):
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())
    assert inc is not None
    return plan_recovery(db, inc, principal=owner).plan


# ------------------------------------------------------------- §37's card
def test_the_card_carries_every_figure_the_spec_draws(db, plan):
    """§37: objective, affected, eligible, expected recovery, budget, status."""
    card = summary(db, plan)

    assert card["objective"]                       # in words, not the enum
    assert card["status"] == plan.status.value
    assert card["affected"] > 0
    assert card["eligible"] <= card["affected"]
    assert card["expected_recovery_minor"] >= 0
    assert card["expected_recovery_basis"]         # never a bare figure
    assert card["budget"]["max_recovery_minor"] > 0
    assert card["budget"]["max_attempts_per_customer"] > 0


def test_affected_is_every_candidate_and_eligible_is_a_subset(db, plan):
    total = db.query(RecoveryCandidate).filter_by(plan_id=plan.id).count()
    eligible = db.query(RecoveryCandidate).filter_by(
        plan_id=plan.id, status=CandidateStatus.ELIGIBLE).count()

    card = summary(db, plan)
    assert card["affected"] == total
    assert card["eligible"] == eligible
    # Every candidate lands in exactly one bucket, so the buckets sum to the total.
    buckets = sum(card[k] for k in ("eligible", "ineligible", "attempted",
                                    "skipped"))
    assert buckets == total


def test_expected_and_recovered_are_never_the_same_field(db, plan):
    """§49. A sent payment link is not money back."""
    card = summary(db, plan)
    assert "expected_recovery_minor" in card
    assert "recovered_minor" in card
    assert card["recovered_minor"] == 0        # nothing has been executed yet


# ------------------------------------------------------- §38's consumption
def test_the_budget_reports_what_has_been_spent_against_each_bound(db, plan):
    card = summary(db, plan)
    b = card["budget"]
    for limit, used in (("max_recovery_minor", "spent_minor"),
                        ("max_actions", "actions_taken"),
                        ("max_duration_seconds", "elapsed_seconds")):
        assert limit in b and used in b, f"{limit} has no consumption reading"
    assert b["spent_minor"] == 0
    assert b["actions_taken"] == 0


def test_an_attempt_counts_against_the_budget_even_when_it_fails(db, plan):
    """A failed attempt still spent an action and still reached a customer.

    A budget that only counted successes would let a campaign retry
    indefinitely at no recorded cost, which is the opposite of a bound.
    """
    candidates = (db.query(RecoveryCandidate)
                  .filter_by(plan_id=plan.id, status=CandidateStatus.ELIGIBLE)
                  .order_by(RecoveryCandidate.rank).limit(2).all())
    assert len(candidates) == 2

    candidates[0].status = CandidateStatus.RECOVERED
    candidates[0].actual_recovery_minor = candidates[0].attributed_amount_minor
    candidates[1].status = CandidateStatus.FAILED
    db.flush()

    b = summary(db, plan)["budget"]
    assert b["actions_taken"] == 2                 # both, not just the success
    assert b["spent_minor"] == (candidates[0].attributed_amount_minor
                                + candidates[1].attributed_amount_minor)


def test_exhausted_names_the_bounds_already_used_up(db, plan):
    assert summary(db, plan)["exhausted"] == []

    plan.max_actions = 1
    c = (db.query(RecoveryCandidate)
         .filter_by(plan_id=plan.id, status=CandidateStatus.ELIGIBLE).first())
    c.status = CandidateStatus.ATTEMPTED
    db.flush()

    assert "max_actions" in summary(db, plan)["exhausted"]


def test_exhausted_reports_and_does_not_decide(db, plan):
    """The stopping rules are the authority. A second decider is a second
    place for the two to disagree."""
    plan.max_actions = 0
    db.flush()
    card = summary(db, plan)
    assert "max_actions" in card["exhausted"]
    # Reporting a used-up bound does not itself stop the campaign.
    assert card["status"] == plan.status.value
    assert plan.stop_rule is None


# ---------------------------------------------------------- §38's fifth bound
def test_the_risk_ceiling_is_carried_by_the_campaign(db, plan):
    """v2 §38: "Every campaign must have explicit limits."

    It was enforced before this as a module constant, which is a real control
    and not an explicit limit: an approver reading a plan could not tell what
    risk it was authorised to take without reading the source.
    """
    from app.recovery.stopping import MAX_UNATTENDED_RISK

    assert plan.max_risk_level == MAX_UNATTENDED_RISK
    assert summary(db, plan)["budget"]["max_risk_level"] == plan.max_risk_level


def test_a_plans_risk_bound_is_fixed_at_creation(db, plan, monkeypatch):
    """Lowering the global ceiling must not retighten a campaign in flight,
    and raising it must not widen one — the same rule the other four follow."""
    import app.recovery.stopping as stopping

    monkeypatch.setattr(stopping, "MAX_UNATTENDED_RISK", "LOW")
    db.refresh(plan)
    assert plan.max_risk_level == "HIGH"


# ------------------------------------------------------------------ listing
def test_the_console_lists_only_campaigns_that_can_still_do_something(db, plan):
    assert plan.id in {p.id for p in active_campaigns(db, "MERCH_A")}

    plan.status = PlanStatus.STOPPED
    db.flush()
    assert plan.id not in {p.id for p in active_campaigns(db, "MERCH_A")}


def test_campaigns_do_not_cross_merchants(db, plan):
    assert active_campaigns(db, "MERCH_B") == []
