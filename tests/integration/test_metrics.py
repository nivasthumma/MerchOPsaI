"""Operational metrics and SLOs — MerchantOps §59, §60.

The assertions that matter are not that numbers appear. They are that a metric
is either measured or absent-with-a-reason, and that the two correctness
objectives would actually catch a violation rather than being trivially zero.
"""
from __future__ import annotations

from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime
from app.detection import detect
from app.metrics import SLO_POLICY_DECISION_MS, objectives, operational_metrics


def _exercise(db, owner):
    detect(db, "MERCH_A")
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    approve_and_execute(db, out.task.id, owner)
    return out


def _by_name(payload):
    return {m["name"]: m for m in payload["available"] + payload["unavailable"]}


# ---------------------------------------------------------------------- §59
def test_every_metric_is_either_measured_or_explained(db, owner):
    _exercise(db, owner)
    payload = operational_metrics(db, "MERCH_A")
    for m in payload["available"]:
        assert m["value"] is not None, f"{m['name']} is listed available with no value"
    for m in payload["unavailable"]:
        assert m["value"] is None, f"{m['name']} is unavailable but carries a value"
        assert m["reason"], f"{m['name']} is unavailable with no reason given"


def test_the_metrics_that_need_ground_truth_say_so_rather_than_guessing(db, owner):
    """A figure computed from nothing is worse than a blank: the blank prompts
    the question and the number closes it."""
    _exercise(db, owner)
    m = _by_name(operational_metrics(db, "MERCH_A"))
    for name in ("root_cause_accuracy", "revenue_at_risk_accuracy", "agent_cost"):
        assert m[name]["available"] is False
        assert m[name]["value"] is None
        assert len(m[name]["reason"]) > 40, f"{name}'s reason is not an explanation"


def test_the_latencies_that_were_being_discarded_are_now_recorded(db, owner):
    """Provider and verification time were both being spent and neither
    recorded — the cheapest kind of missing metric."""
    _exercise(db, owner)
    m = _by_name(operational_metrics(db, "MERCH_A"))
    for name in ("provider_latency_p50", "verification_latency_p50",
                 "detection_latency_p50", "policy_decision_p95"):
        assert m[name]["available"], f"{name} is still not measured"
        assert m[name]["value"] >= 0
        assert m[name]["sample_size"] > 0


def test_metrics_are_merchant_scoped(db, owner):
    _exercise(db, owner)
    a = _by_name(operational_metrics(db, "MERCH_A"))
    b = _by_name(operational_metrics(db, "MERCH_B"))
    assert a["investigation_latency_p50"]["available"]
    assert not b["investigation_latency_p50"]["available"]
    assert b["investigation_latency_p50"]["reason"]


# ---------------------------------------------------------------------- §60
def test_the_policy_slo_is_actually_measured(db, owner):
    """It was the one objective nothing timed. An SLO nobody measures is a wish."""
    _exercise(db, owner)
    o = {x["name"]: x for x in objectives(db, "MERCH_A")}
    slo = o["policy_decision_latency"]
    assert slo["measured"] is not None
    assert slo["holds"] is True
    assert slo["measured"] < SLO_POLICY_DECISION_MS


def test_all_four_objectives_hold_on_a_clean_run(db, owner):
    _exercise(db, owner)
    for o in objectives(db, "MERCH_A"):
        assert o["holds"] is True, f"{o['name']} does not hold: {o}"


def test_an_unapproved_action_fails_the_unauthorized_objective(db, owner):
    """The check must be capable of failing. An objective that reads zero
    because nothing could ever make it non-zero is not measuring anything."""
    out = _exercise(db, owner)
    db.execute(text("UPDATE agent_actions SET approval_id = NULL WHERE task_id = :t"),
               {"t": out.task.id})
    db.flush()

    o = {x["name"]: x for x in objectives(db, "MERCH_A")}
    assert o["unauthorized_executions"]["holds"] is False
    assert o["unauthorized_executions"]["measured"] == 1

    m = _by_name(operational_metrics(db, "MERCH_A"))
    assert m["policy_violations"]["value"] == 1
    assert m["actions_without_an_approval"]["value"] == 1


def test_a_confirmed_action_without_a_verified_success_fails_its_objective(db, owner):
    """§60 calls this one of the two most important. CONFIRMED means an
    independent read-back said SUCCESS; anything else claiming CONFIRMED is the
    system asserting something it did not establish."""
    out = _exercise(db, owner)
    db.execute(text("UPDATE agent_actions SET verification_state = 'UNKNOWN' "
                    "WHERE task_id = :t"), {"t": out.task.id})
    db.flush()

    o = {x["name"]: x for x in objectives(db, "MERCH_A")}
    assert o["unverified_success_claims"]["holds"] is False
    assert o["unverified_success_claims"]["measured"] == 1


def test_an_objective_with_nothing_to_measure_reports_unknown_not_pass(db):
    """A latency objective on an empty database must not read as satisfied.
    Nothing happened, which is not the same as everything having been fast."""
    o = {x["name"]: x for x in objectives(db, "MERCH_B")}
    assert o["policy_decision_latency"]["holds"] is None
    assert o["policy_decision_latency"]["measured"] is None
    assert "No policy decision" in o["policy_decision_latency"]["detail"]
    # The correctness objectives DO hold on an empty database: zero actions
    # means zero unauthorised ones, and that is a true statement.
    assert o["unauthorized_executions"]["holds"] is True


def test_the_endpoints_expose_both(db, owner):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    _exercise(db, owner)
    db.commit()
    sec.reset_rate_limits()
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}
        met = c.get("/metrics/operational", headers=h).json()
        slo = c.get("/metrics/objectives", headers=h).json()
    sec.reset_rate_limits()
    assert met["available"] and met["unavailable"]
    assert met["note"]
    assert len(slo["objectives"]) == 4
