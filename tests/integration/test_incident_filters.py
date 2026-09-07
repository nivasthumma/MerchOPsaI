"""Incident filtering and saved views — plan P1-05.

The column filters (severity, status, type) are easy either way. The ones worth
testing are the *derived* ones — approval required, UNKNOWN, escalated — because
they are facts about the actions an incident produced, reached through the task
that produced them, and a client cannot compute them without fetching the whole
action table.
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import security as sec
from app.api.main import app


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str = "USR_A_OWNER") -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


@pytest.fixture
def incidents(db, owner):
    """Detect against the seeded data, then attach a recovery plan to each so
    the payment-method filter has something to reach through."""
    from app.detection.engine import detect, open_incidents
    from app.recovery.planner import plan_recovery

    detect(db, owner.merchant_id)
    db.flush()
    for inc in open_incidents(db, owner.merchant_id):
        with contextlib.suppress(Exception):
            # Not every incident type has a planned intervention, and this
            # fixture wants whatever the system actually produces rather than a
            # curated subset of it.
            plan_recovery(db, inc, principal=owner)
    db.flush()
    return open_incidents(db, owner.merchant_id)


def _ids(body) -> list[str]:
    return [i["id"] for i in body["incidents"]]


# ------------------------------------------------------------- column filters
def test_the_unfiltered_call_is_unchanged(client, db, incidents):
    """Nothing that used this endpoint before pays for filtering it does not
    use, and it still returns what it always did."""
    body = client.get("/incidents", headers=token()).json()
    assert len(body["incidents"]) == len(incidents)
    assert body["applied_view"] is None


def test_severity_filters_in_sql(client, db, incidents):
    body = client.get("/incidents?severity=CRITICAL", headers=token()).json()
    assert all(i["severity"] == "CRITICAL" for i in body["incidents"])

    # And a multi-valued filter is a union, not the last value winning.
    both = client.get("/incidents?severity=CRITICAL&severity=HIGH",
                      headers=token()).json()
    assert {i["severity"] for i in both["incidents"]} <= {"CRITICAL", "HIGH"}
    assert len(both["incidents"]) >= len(body["incidents"])


def test_amount_and_age_filter(client, db, incidents):
    biggest = max(i.revenue_at_risk_minor for i in incidents)
    body = client.get(f"/incidents?min_amount_minor={biggest}",
                      headers=token()).json()
    assert all(i["revenue_at_risk_minor"] >= biggest for i in body["incidents"])

    # Age is measured from DETECTION: an incident whose degradation began last
    # week but which was detected an hour ago is an hour old to whoever acts.
    assert len(client.get("/incidents?max_age_hours=1", headers=token())
               .json()["incidents"]) == len(incidents)
    db.execute(text("UPDATE incidents SET detected_at = now() - interval '3 days'"))
    db.flush()
    assert client.get("/incidents?max_age_hours=1", headers=token()) \
        .json()["incidents"] == []


def test_an_incident_with_no_plan_matches_no_payment_method(client, db, incidents):
    """Correct rather than convenient: nothing has attributed it to a method."""
    db.execute(text("DELETE FROM recovery_candidates"))
    db.flush()
    body = client.get("/incidents?payment_method=upi", headers=token()).json()
    assert body["incidents"] == []


# ------------------------------------------------------------ derived filters
def test_approval_required_finds_incidents_gated_on_a_person(client, db, owner, incidents):
    from app.agent.runtime import AgentRuntime

    before = _ids(client.get("/incidents?approval_required=true",
                             headers=token()).json())
    assert before == [], "nothing is gated before anything has been proposed"

    inc = incidents[0]
    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    assert out.approval is not None
    out.task.incident_id = inc.id
    db.flush()

    assert inc.id in _ids(client.get("/incidents?approval_required=true",
                                     headers=token()).json())


def test_an_expired_approval_does_not_count_as_waiting_on_anyone(client, db, owner, incidents):
    """It cannot execute. Listing it as work waiting on a person invites an
    operator to try."""
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    out.task.incident_id = incidents[0].id
    db.execute(text("UPDATE approvals SET expires_at = now() - interval '1 hour'"))
    db.flush()

    assert _ids(client.get("/incidents?approval_required=true",
                           headers=token()).json()) == []


def test_unknown_and_escalated_are_distinguished(client, db, owner, incidents):
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.reconciler import escalate_exhausted

    inc = incidents[0]
    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    out.task.incident_id = inc.id
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    db.flush()

    assert inc.id in _ids(client.get("/incidents?has_unknown=true", headers=token()).json())
    assert _ids(client.get("/incidents?escalated=true", headers=token()).json()) == []

    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": r["action"].id})
    db.expire_all()
    escalate_exhausted(db)
    db.flush()

    # It moves from one filter to the other. An escalated action is still
    # unsettled, but it is no longer being worked automatically, and the two
    # filters answer different questions.
    assert inc.id in _ids(client.get("/incidents?escalated=true", headers=token()).json())
    assert _ids(client.get("/incidents?has_unknown=true", headers=token()).json()) == []


# --------------------------------------------------------------- saved views
def test_every_declared_view_resolves_and_is_counted(client, db, incidents):
    from app.incidents.filters import SAVED_VIEWS

    body = client.get("/incidents", headers=token()).json()
    assert [v["key"] for v in body["views"]] == [v["key"] for v in SAVED_VIEWS]
    for v in body["views"]:
        assert v["label"] and v["hint"]
        assert isinstance(v["count"], int)
        # And the count matches what asking for the view actually returns.
        got = client.get(f"/incidents?view={v['key']}", headers=token()).json()
        assert len(got["incidents"]) == v["count"], v["key"]


def test_my_attention_is_an_or_not_an_and(client, db, owner, incidents):
    """The one view whose predicates are ORed: approval required OR escalated.
    ANDed it would need both at once and would almost never match."""
    from app.agent.runtime import AgentRuntime

    assert client.get("/incidents?view=my_attention", headers=token()) \
        .json()["incidents"] == [], "open alone is not 'my attention'"

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    out.task.incident_id = incidents[0].id
    db.flush()

    got = _ids(client.get("/incidents?view=my_attention", headers=token()).json())
    assert incidents[0].id in got


def test_a_view_that_matched_everything_would_not_be_a_view(client, db, incidents):
    """`my_attention` must not degenerate into the unfiltered list."""
    everything = len(client.get("/incidents", headers=token()).json()["incidents"])
    attention = len(client.get("/incidents?view=my_attention",
                               headers=token()).json()["incidents"])
    assert everything > 0
    assert attention < everything


def test_an_unknown_view_is_refused_rather_than_ignored(client, db):
    """Silently showing everything would read as though the view matched every
    incident."""
    r = client.get("/incidents?view=nonexistent", headers=token())
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "unknown_view"
    assert "my_attention" in detail["known"]


def test_the_applied_view_is_echoed_back(client, db, incidents):
    body = client.get("/incidents?view=critical", headers=token()).json()
    assert body["applied_view"] == "critical"


def test_filters_cannot_reach_another_merchant(client, db, incidents):
    """Merchant scope is the first clause of every query here, never a filter
    applied afterwards."""
    for q in ("", "?view=my_attention", "?severity=CRITICAL", "?has_unknown=true",
              "?min_amount_minor=0", "?unresolved=true"):
        body = client.get(f"/incidents{q}", headers=token("USR_B_OWNER")).json()
        assert all(i["merchant_id"] == "MERCH_B" for i in body["incidents"]), q
