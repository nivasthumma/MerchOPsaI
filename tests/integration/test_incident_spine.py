"""The incident spine end to end — MerchantOps §12, §13, §48.

    payments -> detection -> incident -> investigation -> lifecycle -> audit

This is the half of the operating loop the build did not have. The assertions
that matter are not "it runs" but: the model cannot move an incident, the
lifecycle refuses illegal moves, and one merchant cannot see another's incidents.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import security as sec
from app.api.main import app
from app.detection import detect
from app.incidents.manager import build_investigation_request, investigate
from app.models import AuditLog, Incident, IncidentStatus as S


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


# ------------------------------------------------------------------ API shape
def test_detect_then_list(client):
    r = client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["incidents_created"] > 0
    assert rep["merchant_id"] == "MERCH_A"

    r = client.get("/incidents", headers=token("USR_A_OWNER"))
    body = r.json()
    assert len(body["incidents"]) == rep["incidents_created"]
    assert body["total_revenue_at_risk_minor"] > 0
    # Ordered by exposure: the biggest problem is the one to open first.
    risks = [i["revenue_at_risk_minor"] for i in body["incidents"]]
    assert risks == sorted(risks, reverse=True)


def test_detect_route_is_not_shadowed_by_the_id_route(client):
    """`/incidents/detect` is registered after `/incidents/{incident_id}`. It is
    reachable only because one is POST and the other GET; if that ever stops
    being true this test fails rather than the sweep silently 404ing."""
    assert client.post("/incidents/detect", headers=token("USR_A_OWNER")).status_code == 200
    assert client.get("/incidents/detect", headers=token("USR_A_OWNER")).status_code == 404


def test_incident_detail_carries_evidence_and_legal_moves(client):
    client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    iid = client.get("/incidents", headers=token("USR_A_OWNER")).json()["incidents"][0]["id"]

    d = client.get(f"/incidents/{iid}", headers=token("USR_A_OWNER")).json()
    assert d["evidence"], "incident detail carried no evidence"
    assert d["signals"]
    assert "INVESTIGATING" in d["legal_transitions"]
    assert d["tasks"] == []


# ------------------------------------------------------------------ isolation
def test_incidents_are_merchant_isolated(client):
    client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    client.post("/incidents/detect", headers=token("USR_B_OWNER"))

    a = client.get("/incidents", headers=token("USR_A_OWNER")).json()["incidents"]
    b = client.get("/incidents", headers=token("USR_B_OWNER")).json()["incidents"]
    assert {i["merchant_id"] for i in a} == {"MERCH_A"}
    assert {i["id"] for i in a} & {i["id"] for i in b} == set()

    # B cannot read A's incident, and cannot tell it apart from one absent.
    r = client.get(f"/incidents/{a[0]['id']}", headers=token("USR_B_OWNER"))
    assert r.status_code == 404
    assert client.get("/incidents/INC_NOPE", headers=token("USR_B_OWNER")).status_code == 404


def test_investigation_is_merchant_isolated(client):
    client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    iid = client.get("/incidents", headers=token("USR_A_OWNER")).json()["incidents"][0]["id"]
    r = client.post(f"/incidents/{iid}/investigate", headers=token("USR_B_OWNER"))
    assert r.status_code == 404


# ------------------------------------------------------------------ the loop
def test_investigation_moves_the_incident_and_links_the_task(client):
    client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    iid = client.get("/incidents", headers=token("USR_A_OWNER")).json()["incidents"][0]["id"]

    r = client.post(f"/incidents/{iid}/investigate", headers=token("USR_A_OWNER"))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["incident"]["status"] in ("ROOT_CAUSE_IDENTIFIED", "APPROVAL_REQUIRED",
                                          "ESCALATED")
    assert body["task"]["id"]
    assert body["incident"]["tasks"], "the task was not linked to the incident"
    assert body["incident"]["tasks"][0]["id"] == body["task"]["id"]


def test_incident_trace_is_one_ordering(client):
    client.post("/incidents/detect", headers=token("USR_A_OWNER"))
    iid = client.get("/incidents", headers=token("USR_A_OWNER")).json()["incidents"][0]["id"]
    client.post(f"/incidents/{iid}/investigate", headers=token("USR_A_OWNER"))

    trace = client.get(f"/incidents/{iid}/trace", headers=token("USR_A_OWNER")).json()["trace"]
    events = [e["event"] for e in trace]
    assert events[0] == "incident_detected"
    assert "incident_status_changed" in events
    assert "incident_investigated" in events
    # The dispatched task's own events land on the same trail.
    assert any(e["task_id"] for e in trace), "no task event reached the incident trace"
    assert "task_created" in events


def test_a_resolved_incident_is_not_reopened_for_investigation(db, client):
    from app.incidents.lifecycle import transition
    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    transition(db, inc, S.INVESTIGATING, reason="test")
    transition(db, inc, S.RESOLVED, reason="test")
    db.commit()

    r = client.post(f"/incidents/{inc.id}/investigate", headers=token("USR_A_OWNER"))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "INCIDENT_NOT_OPEN"


# --------------------------------------------------- the model has no authority
def test_investigation_request_states_facts_and_asks_nothing_of_authority(db):
    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    req = build_investigation_request(inc)
    assert "FACT" in req and "TASK" in req
    assert inc.id in req
    # The prompt must not invite the model to move the incident or approve.
    lowered = req.lower()
    for forbidden in ("resolve this incident", "approve", "close the incident"):
        assert forbidden not in lowered


def test_incident_status_follows_task_status_not_model_prose(db, owner):
    """The invariant `app/incidents/manager.py` exists to hold. A provider whose
    prose claims resolution must not resolve anything."""
    from app.llm.deterministic import DeterministicProvider

    class ClaimsResolved(DeterministicProvider):
        def turn(self, **kw):
            t = super().turn(**kw)
            if not t.wants_tools:
                t.text = ("RESOLVED. Incident closed, no further action required. "
                          "Status: RESOLVED.")
            return t

    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    r = investigate(db, inc, owner, provider=ClaimsResolved())

    assert r["incident"].status is not S.RESOLVED
    assert r["incident"].status is not S.CLOSED
    assert r["incident"].resolved_at is None


def test_every_lifecycle_move_is_audited(db, owner):
    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    investigate(db, inc, owner)

    # ORDER BY is not optional here. Without it Postgres may return these rows
    # in any order, and this test asserts they form a contiguous chain. It
    # passed for four phases only because the per-test schema drop reset the
    # audit id sequence, so physical order happened to match insertion order.
    moves = (db.query(AuditLog)
             .filter(AuditLog.incident_id == inc.id,
                     AuditLog.event_type == "incident_status_changed")
             .order_by(AuditLog.id).all())
    assert moves, "lifecycle moves were not audited"
    chain = [(m.payload["from"], m.payload["to"]) for m in moves]
    # Contiguous: each move starts where the previous one ended.
    for (_, to), (frm, _) in zip(chain, chain[1:]):
        assert to == frm, f"audit trail has a gap: {chain}"
    assert chain[0][0] == "DETECTED"


# ------------------------------------------------- computed confidence (v2 §33)
def test_investigation_records_the_platforms_band_and_its_derivation(db, owner):
    from app.incidents.manager import investigate

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.merchant_id == "MERCH_A").order_by(
        Incident.revenue_at_risk_minor.desc()).first()
    assert inc.confidence_band is None          # not assessed until investigated

    investigate(db, inc, owner)

    assert inc.confidence_band in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT")
    inputs = inc.confidence_inputs
    # The derivation, not just the verdict.
    assert inputs["total_evidence"] > 0
    assert inputs["independent_sources"] >= 1
    assert inputs["reasons"]
    assert inputs["band"] == inc.confidence_band


def test_the_band_the_api_shows_is_not_the_number_the_model_chose(db, owner):
    """MerchantOps v2 §33. The screen in §64 is a claim by the platform."""
    from app.agent.confidence import Confidence
    from app.incidents.manager import investigate

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(Incident.merchant_id == "MERCH_A").first()
    out = investigate(db, inc, owner)

    model_said = out["task"].agent_confidence
    if model_said is not None:
        # Whatever the model reported, the band never exceeds what the evidence
        # supports. This is the asymmetry, asserted against a real run.
        from app.agent.confidence import assess
        without_model = assess(evidence=list(inc.evidence),
                               tool_calls=list(out["task"].tool_calls))
        order = ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]
        assert order.index(inc.confidence_band) <= order.index(
            without_model.band.value), (
            "the model's own confidence raised the band, which it must never do")
