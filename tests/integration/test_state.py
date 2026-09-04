"""The merchant digital twin — MerchantOps v2 §14, ADR-0040.

Two assertions carry this file.

The first is that the twin **agrees with the modules it assembles**. §14 is a
view over figures other code already owns, and the failure it invites is two
answers to one question: a dashboard quoting one revenue number while the tool
the agent reads quotes another. Every branch is checked against its owner.

The second is that a branch nothing can measure says so. §14 lists a Payments
latency this system does not collect, and a zero there would be a lie that reads
as a measurement.
"""
from __future__ import annotations

import pytest

from app.detection import detect
from app.metrics import operational_metrics
from app.models import Incident, IncidentType
from app.recovery.ledger import build_ledger, dashboard
from app.state import PERIOD_DAYS, build_state
from app.tools.investigation import get_payment_metrics, get_revenue_summary


@pytest.fixture
def state(db):
    return build_state(db, "MERCH_A")


# ------------------------------------------------ §14's tree, all six branches
def test_the_twin_has_every_branch_section_14_names(state):
    d = state.as_dict()
    for branch in ("financial", "payments", "customers", "incidents",
                   "recovery", "operational_health"):
        assert branch in d, branch
    assert d["merchant_id"] == "MERCH_A"
    assert d["period_days"] == PERIOD_DAYS
    # Computed per read, so it says when. A figure with no as-of is one somebody
    # quotes an hour later.
    assert d["as_of"]


# --------------------------------------- it agrees with what it assembles
def test_revenue_agrees_with_the_tool_the_agent_reads(db, state):
    """The failure this test exists for: a dashboard and an agent quoting
    different revenue for the same period."""
    tool = get_revenue_summary(db, "MERCH_A").data
    fin = state.financial.values
    assert fin["current_period_revenue_minor"] == tool["current_period_revenue_minor"]
    assert fin["previous_period_revenue_minor"] == tool["previous_period_revenue_minor"]


def test_method_health_agrees_with_the_payment_metrics_tool(db, state):
    tool = {m["method"]: m for m in get_payment_metrics(db, "MERCH_A").data["by_method"]}
    for m in state.payments.values["by_method"]:
        assert m["success_rate_pct"] == tool[m["method"]]["current_success_rate_pct"]
        assert m["attempts"] == tool[m["method"]]["current_total"]


def test_revenue_at_risk_is_read_from_the_ledger_not_recomputed(db, state):
    """§22 owns this figure. Recomputing it beside its owner is how two answers
    to one question get created."""
    assert (state.financial.values["revenue_at_risk_minor"]
            == build_ledger(db, "MERCH_A").at_risk_minor)


def test_incidents_and_recovery_are_the_dashboard_s_own(db, state):
    board = dashboard(db, "MERCH_A")
    assert state.incidents.values == board["incidents"]
    assert state.recovery.values == board["recovery"]


def test_operational_health_carries_the_metrics_registry(db, state):
    assert (state.operational_health.values["metrics"]
            == operational_metrics(db, "MERCH_A"))


# ------------------------------------------------- GMV is not revenue
def test_gmv_is_attempted_value_and_revenue_is_captured(db, state):
    """§14 lists both, and the ratio is the conversion story. Reporting GMV as
    a bigger revenue number would tell the opposite one.

    Pinned to the arithmetic rather than bounded. An earlier version asserted
    `gmv >= revenue` and `capture_rate <= 100`, and a mutation setting
    `gmv = revenue` SURVIVED it — both hold when the two are equal. A bound is
    not a test of a quantity that is supposed to differ.
    """
    from datetime import timedelta

    from sqlalchemy import text

    from app.state import PERIOD_DAYS
    from scripts.seed_data import ANCHOR

    fin = state.financial.values
    expected = db.execute(text("""
        SELECT COALESCE(SUM(amount_minor), 0)                         AS gmv,
               COALESCE(SUM(amount_minor) FILTER (
                   WHERE status <> 'failed'), 0)                      AS revenue,
               COALESCE(SUM(amount_minor) FILTER (
                   WHERE status = 'failed'), 0)                       AS failed
        FROM payments WHERE merchant_id = 'MERCH_A' AND created_at >= :cut
    """), {"cut": ANCHOR - timedelta(days=PERIOD_DAYS)}).mappings().one()

    assert fin["gmv_minor"] == int(expected["gmv"])
    # GMV is revenue PLUS what was attempted and not captured. The seeded data
    # has failures, so the two genuinely differ and the gap is measurable.
    assert int(expected["failed"]) > 0, "the fixture no longer exercises the gap"
    assert fin["gmv_minor"] == fin["current_period_revenue_minor"] + int(expected["failed"])
    assert fin["gmv_minor"] > fin["current_period_revenue_minor"]
    assert fin["capture_rate_pct"] < 100.0
    # The definition travels with the figure, so it cannot be read as revenue.
    assert "captured" in fin["gmv_definition"]


def test_the_seeded_revenue_decline_is_visible(state):
    """The dataset plants a decline. A twin that reported growth would be
    assembling the wrong window."""
    assert state.financial.values["revenue_change_pct"] < 0


# ------------------------------------------ what cannot be measured says so
def test_payment_latency_reports_unmeasured_rather_than_zero(state):
    """§14 names it; `payments` has a created_at and nothing to subtract.

    A zero here would read as "payments are instant", which is a claim nobody
    made. `agent_actions.provider_latency_ms` is a different quantity — this
    system's own provider calls — and using it would be worse than a blank.
    """
    latency = state.payments.values["latency"]
    assert latency["measured"] is False
    assert "not collected" in latency["reason"]
    assert "value" not in latency and "ms" not in latency


def test_an_unmeasured_branch_renders_its_reason(state):
    from app.state import Branch

    b = Branch({}, measured=False, unmeasured_reason="nothing to read")
    d = b.as_dict()
    assert d["measured"] is False and d["reason"] == "nothing to read"


# --------------------------------------------- the agent's slice (§14, §26)
def test_the_agent_gets_a_slice_and_not_the_whole_state(db, state):
    """§26: do not send the entire database to the model. Handing over the
    full twin would be a context bill, not a context strategy."""
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())

    slice_ = state.for_incident(inc)
    assert len(slice_) < len(state.as_dict())
    # No operational metrics, no agent activity, no full ledger.
    assert "operational_health" not in slice_
    assert "recovery" not in slice_


def test_the_slice_narrows_to_the_incident_s_own_method(db, state):
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())
    method = inc.signals["method"]

    health = state.for_incident(inc)["method_health"]
    assert [m["method"] for m in health] == [method], (
        "the slice carried methods this incident is not about")


def test_an_incident_with_no_method_gets_every_method(db, state):
    """A duplicate payment is not about a rail, so narrowing to one would drop
    the context rather than focus it."""
    detect(db, "MERCH_A")
    dupe = (db.query(Incident)
            .filter(Incident.incident_type == IncidentType.DUPLICATE_PAYMENT)
            .first())
    health = state.for_incident(dupe)["method_health"]
    assert len(health) == len(state.payments.values["by_method"])


# ------------------------------------------------------------------ tenancy
def test_the_twin_does_not_cross_merchants(db):
    a = build_state(db, "MERCH_A").as_dict()
    b = build_state(db, "MERCH_B").as_dict()
    assert a["merchant_id"] == "MERCH_A" and b["merchant_id"] == "MERCH_B"
    assert a["financial"] != b["financial"]
    assert a["customers"]["active"] != b["customers"]["active"]


def test_the_endpoint_is_scoped_by_the_bearer_token(db):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        a = c.get("/state", headers={
            "Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"})
        b = c.get("/state", headers={
            "Authorization": f"Bearer {sec.issue_token('USR_B_OWNER')}"})
    sec.reset_rate_limits()

    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["merchant_id"] == "MERCH_A"
    assert b.json()["merchant_id"] == "MERCH_B"
