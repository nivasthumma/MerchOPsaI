"""Competing hypotheses tested against evidence — MerchantOps v2 §30.

The assertions that carry §30 are the ones about *failure*: a hypothesis that
cannot be contradicted is a guess with ceremony, so the tests that matter are
the ones showing a probe rejecting a candidate on real data, and the one
showing a probe changing its mind when the data changes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.detection import detect
from app.evidence.hypotheses import (
    adjudicate,
    candidates_for,
    for_incident,
    leading,
    propose,
)
from app.models import (
    EventOutbox,
    EvidenceEdge,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentType,
    Predicate,
)


@pytest.fixture
def degradation(db) -> Incident:
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.merchant_id == "MERCH_A",
                   Incident.incident_type == IncidentType.PAYMENT_DEGRADATION)
           .first())
    assert inc is not None
    return inc


# ------------------------------------------------------- §30's worked example
def test_the_evidence_settles_which_explanation_survives(db, degradation):
    """v2 §30's own figure, reproduced from the seeded data.

        provider evidence      supports H1
        merchant configuration untestable
        traffic                normal      → rejected
        customer segment       not concentrated → rejected
    """
    hypotheses = {h.key: h for h in adjudicate(db, degradation)}
    assert set(hypotheses) == {"provider_degradation", "merchant_configuration",
                               "traffic_anomaly", "customer_segment"}

    assert hypotheses["provider_degradation"].status is HypothesisStatus.SUPPORTED
    assert hypotheses["traffic_anomaly"].status is HypothesisStatus.REJECTED
    assert hypotheses["customer_segment"].status is HypothesisStatus.REJECTED
    assert hypotheses["merchant_configuration"].status is HypothesisStatus.UNTESTED

    assert leading(db, degradation.id).key == "provider_degradation"


def test_every_verdict_states_its_reason(db, degradation):
    """A rejected hypothesis with no stated reason is an assertion."""
    for h in adjudicate(db, degradation):
        assert h.verdict_reason, f"{h.key} has no reason"
        assert h.adjudicated_at is not None


# -------------------------------------------------- the probes actually probe
def test_a_probe_changes_its_mind_when_the_data_changes(db, degradation):
    """The test that separates a hypothesis engine from a list of guesses.

    `traffic_anomaly` is rejected on the seeded data because UPI attempt volume
    genuinely did not move. Delete most of the earlier window and volume *has*
    moved, and the same probe supports it instead. If this test could not be
    written, the verdicts above would be constants wearing a query.
    """
    before = {h.key: h.status for h in adjudicate(db, degradation)}
    assert before["traffic_anomaly"] is HypothesisStatus.REJECTED

    # Add a genuine volume spike inside the incident window, cloned from a real
    # row so every foreign key still resolves. Inserting rather than deleting:
    # `payments` is referenced by `refunds`, and a test that has to disable a
    # constraint to make its point is testing a database that does not exist.
    db.execute(text("""
        INSERT INTO payments (id, merchant_id, order_id, customer_id,
                              amount_minor, currency, method, status,
                              amount_refunded_minor, created_at)
        SELECT 'SPIKE_' || g, p.merchant_id, p.order_id, p.customer_id,
               p.amount_minor, p.currency, 'upi', 'captured', 0, :start
        FROM (SELECT * FROM payments
              WHERE merchant_id = 'MERCH_A' AND method = 'upi' LIMIT 1) p,
             generate_series(1, 400) g
    """), {"start": degradation.started_at})
    db.flush()

    after = {h.key: h.status for h in adjudicate(db, degradation)}
    assert after["traffic_anomaly"] is not HypothesisStatus.REJECTED, \
        "the probe returned the same verdict on different data"

    # Two explanations now fit, so NEITHER is promoted. That is the second
    # thing §30 buys: a single-shot answer would have named the first one, and
    # CONTENDING says out loud that the evidence does not choose between them.
    assert after["traffic_anomaly"] is HypothesisStatus.CONTENDING
    assert after["provider_degradation"] is HypothesisStatus.CONTENDING
    assert leading(db, degradation.id) is None


def test_an_untestable_hypothesis_is_untested_and_not_rejected(db, degradation):
    """"We cannot look" is not "we looked and found nothing".

    Folding the first into the second hides a gap in instrumentation behind a
    verdict that looks settled — the same error as collapsing UNKNOWN into
    FAILED (§53).
    """
    h = {x.key: x for x in adjudicate(db, degradation)}["merchant_configuration"]
    assert h.status is HypothesisStatus.UNTESTED
    assert h.support_count == 0 and h.contradiction_count == 0
    assert "cannot be tested" in h.verdict_reason


def test_an_incident_type_with_one_explanation_gets_no_ceremony(db):
    """Manufacturing three hypotheses to reject is not reasoning."""
    detect(db, "MERCH_A")
    dupe = (db.query(Incident)
            .filter(Incident.incident_type == IncidentType.DUPLICATE_PAYMENT)
            .first())
    assert dupe is not None
    assert candidates_for(dupe) == ()
    assert adjudicate(db, dupe) == []


# ------------------------------------------------------------- the graph link
def test_each_verdict_is_drawn_into_the_evidence_graph(db, degradation):
    """A rejection has to be walkable, not merely stated — v2 §32.

    This is what `CONTRADICTS` was added to `Predicate` for.
    """
    adjudicate(db, degradation)

    edges = (db.query(EvidenceEdge)
             .filter(EvidenceEdge.incident_id == degradation.id,
                     EvidenceEdge.subject_type == "hypothesis").all())
    assert edges

    supports = [e for e in edges if e.predicate is Predicate.SUPPORTED_BY]
    against = [e for e in edges if e.predicate is Predicate.CONTRADICTS]
    assert supports and against

    for e in edges:
        assert e.drawn_by == "hypothesis_engine"
        assert e.object_value.get("detail")


def test_a_hypothesis_untested_for_lack_of_data_draws_no_edge(db, degradation):
    """An edge is a finding. Nothing was found, so there is nothing to draw."""
    adjudicate(db, degradation)
    untested = {h.key: h for h in for_incident(db, degradation.id)}["merchant_configuration"]
    edges = (db.query(EvidenceEdge)
             .filter(EvidenceEdge.subject_id == untested.id).all())
    assert edges == []


# -------------------------------------------------------------- idempotency
def test_re_adjudicating_re_tests_rather_than_accumulating(db, degradation):
    first = adjudicate(db, degradation)
    n = db.query(Hypothesis).filter_by(incident_id=degradation.id).count()

    second = adjudicate(db, degradation)
    assert db.query(Hypothesis).filter_by(incident_id=degradation.id).count() == n
    assert {h.id for h in first} == {h.id for h in second}


def test_counts_are_recomputed_rather_than_incremented(db, degradation):
    """Otherwise a second pass doubles them and the cache stops matching the graph."""
    adjudicate(db, degradation)
    adjudicate(db, degradation)
    adjudicate(db, degradation)
    for h in for_incident(db, degradation.id):
        assert h.support_count <= 1
        assert h.contradiction_count <= 1


def test_proposing_twice_creates_one_set(db, degradation):
    a = propose(db, degradation)
    b = propose(db, degradation)
    assert {h.id for h in a} == {h.id for h in b}


# ------------------------------------------------------------------- events
def test_proposal_and_rejection_reach_the_live_stream(db, degradation):
    """`hypothesis.created` and `hypothesis.rejected` are two of v2 §62's
    fifteen frames, and until now nothing produced either."""
    adjudicate(db, degradation)

    created = db.query(EventOutbox).filter_by(event_type="hypothesis.created").all()
    rejected = db.query(EventOutbox).filter_by(event_type="hypothesis.rejected").all()
    assert len(created) == 4
    assert len(rejected) == 2                 # traffic and segment

    for row in created + rejected:
        assert row.incident_id == degradation.id
        assert row.merchant_id == degradation.merchant_id
        assert row.payload["key"]
        assert row.payload["statement"]


def test_a_rejection_is_announced_once_not_on_every_pass(db, degradation):
    adjudicate(db, degradation)
    adjudicate(db, degradation)
    rejected = db.query(EventOutbox).filter_by(event_type="hypothesis.rejected").count()
    assert rejected == 2


# ------------------------------------------------- through the real pipeline
def test_investigation_adjudicates_and_the_api_reports_it(db, owner, degradation):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app
    from app.incidents.manager import investigate

    investigate(db, degradation, owner)
    db.commit()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        r = c.get(f"/incidents/{degradation.id}/hypotheses",
                  headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"})
    sec.reset_rate_limits()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leading"] == "provider_degradation"
    assert body["untested"] == ["merchant_configuration"]
    assert len(body["hypotheses"]) == 4
    assert all(h["verdict_reason"] for h in body["hypotheses"])


def test_another_merchant_cannot_read_the_hypotheses(db, owner, degradation):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app
    from app.incidents.manager import investigate

    investigate(db, degradation, owner)
    db.commit()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        r = c.get(f"/incidents/{degradation.id}/hypotheses",
                  headers={"Authorization": f"Bearer {sec.issue_token('USR_B_OWNER')}"})
    sec.reset_rate_limits()
    assert r.status_code == 404, r.text     # absent, not forbidden (§54)
