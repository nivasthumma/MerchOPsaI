"""The evidence graph — MerchantOps v2 §32.

§32 exists to answer "why do you believe this?", so the assertions here are
about whether the answer can be walked back to something checkable: every edge
names who drew it, an ungrounded conclusion gets no edge at all, and redrawing
the graph does not double it.
"""
from __future__ import annotations

import pytest

from app.detection import detect
from app.evidence import build, draw, edges_for, explain, why
from app.models import EvidenceEdge, Incident, Predicate


@pytest.fixture
def incident(db) -> Incident:
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.merchant_id == "MERCH_A")
           .order_by(Incident.revenue_at_risk_minor.desc()).first())
    assert inc is not None
    return inc


# ------------------------------------------------------------------ drawing
def test_the_graph_is_drawn_from_what_is_already_recorded(db, incident):
    n = build(db, incident)
    assert n > 0

    edges = edges_for(db, incident.id)
    assert edges

    # §32's figure: evidence supports, the exposure is created.
    predicates = {e.predicate for e in edges}
    assert Predicate.SUPPORTED_BY in predicates
    assert Predicate.CREATES in predicates

    # Every edge names a deterministic producer. An edge nobody owns is an
    # assertion nobody can be asked about.
    assert all(e.drawn_by for e in edges)
    assert "model" not in {e.drawn_by for e in edges}


def test_the_exposure_edge_points_at_the_calculation_engines_figure(db, incident):
    build(db, incident)
    created = edges_for(db, incident.id, Predicate.CREATES)
    assert len(created) == 1
    edge = created[0]
    assert edge.drawn_by == "calculation_engine"
    # The number is the incident's own, not a restatement of it.
    assert edge.object_value["amount_minor"] == incident.revenue_at_risk_minor


def test_untrusted_evidence_is_included_and_marked_rather_than_hidden(db, incident):
    """MerchantOps §39. Leaving it out hides what was looked at; showing it
    unmarked lets order free text read as corroboration."""
    build(db, incident)
    supported = edges_for(db, incident.id, Predicate.SUPPORTED_BY)
    evidence_edges = [e for e in supported if e.object_type == "evidence"]
    assert evidence_edges
    assert all("untrusted" in (e.object_value or {}) for e in evidence_edges)


# -------------------------------------------------------------- idempotency
def test_redrawing_the_graph_does_not_double_it(db, incident):
    """Re-investigating an incident redraws its graph. It must not grow."""
    first = build(db, incident)
    before = db.query(EvidenceEdge).filter_by(incident_id=incident.id).count()

    second = build(db, incident)
    after = db.query(EvidenceEdge).filter_by(incident_id=incident.id).count()

    assert first > 0
    assert second == 0, "a redraw asserted something new"
    assert after == before


def test_an_edge_whose_object_is_a_quantity_also_deduplicates(db, incident):
    """The NULLS NOT DISTINCT case.

    `object_id` is NULL for every edge pointing at a figure rather than a row —
    the exposure, the affected counts, the root cause. Under the SQL default two
    NULLs are distinct, so exactly these edges would escape the constraint and
    duplicate on every redraw. The edges most likely to be redrawn are the ones
    a plain unique constraint would have missed.
    """
    first = draw(db, incident, predicate=Predicate.CREATES,
                 object_type="revenue_at_risk",
                 object_value={"amount_minor": 100}, drawn_by="calculation_engine")
    assert first is not None
    assert first.object_id is None

    again = draw(db, incident, predicate=Predicate.CREATES,
                 object_type="revenue_at_risk",
                 object_value={"amount_minor": 100}, drawn_by="calculation_engine")
    assert again is None, "a NULL object_id escaped the unique constraint"


def test_a_duplicate_edge_does_not_discard_the_edges_drawn_beside_it(db, incident):
    """The collision is a SAVEPOINT rollback, not a transaction rollback."""
    draw(db, incident, predicate=Predicate.AFFECTS, object_type="customers",
         object_value={"count": 5}, drawn_by="recovery_planner")
    before = db.query(EvidenceEdge).filter_by(incident_id=incident.id).count()

    assert draw(db, incident, predicate=Predicate.AFFECTS,
                object_type="customers", object_value={"count": 5},
                drawn_by="recovery_planner") is None
    # The earlier edge survived the collision.
    assert db.query(EvidenceEdge).filter_by(incident_id=incident.id).count() == before


# ------------------------------------------------------- the model's claims
def test_a_grounded_root_cause_becomes_an_edge(db, incident):
    findings = [{"finding_type": "root_cause", "kind": "INFERRED",
                 "claim": "UPI degradation drove the decline.",
                 "evidence_refs": ["TC_001"]}]
    build(db, incident, findings=findings)

    causes = [e for e in edges_for(db, incident.id, Predicate.CAUSED_BY)
              if e.object_type == "root_cause"]
    assert len(causes) == 1
    assert causes[0].drawn_by == "agent"
    # The edge carries what it was concluded FROM, so a reader can walk back.
    assert causes[0].object_value["evidence_refs"] == ["TC_001"]


def test_an_ungrounded_conclusion_gets_no_edge(db, incident):
    """A conclusion citing nothing is one the graph cannot let a reader walk
    back from, which is the single thing §32 is for."""
    build(db, incident, findings=[
        {"finding_type": "root_cause", "claim": "It was the provider.",
         "evidence_refs": []},
    ])
    causes = [e for e in edges_for(db, incident.id, Predicate.CAUSED_BY)
              if e.object_type == "root_cause"]
    assert causes == []


def test_a_recovery_plan_is_not_a_root_cause(db, incident):
    """The bug this pins shipped and was caught by reading the output.

    `kind` is the storage kind, and INFERRED covers three different model
    finding types plus the recovery planner's own summary. Selecting on it drew
    an edge labelled `caused_by root_cause` whose claim was "intervention
    PAYMENT_LINK, 32 of 33 candidates eligible" — a plan, not a cause. A
    mislabelled edge is worse than a missing one: the missing one prompts the
    question and the mislabelled one answers it wrongly.
    """
    build(db, incident, findings=[
        {"kind": "INFERRED", "finding_type": "recommendation",
         "claim": "Intervention PAYMENT_LINK, 32 of 33 candidates eligible.",
         "evidence_refs": ["TC_001"]},
        {"kind": "INFERRED", "finding_type": "uncertainty",
         "claim": "Provider state could not be established.",
         "evidence_refs": ["TC_002"]},
    ])
    causes = [e for e in edges_for(db, incident.id, Predicate.CAUSED_BY)
              if e.object_type == "root_cause"]
    assert causes == []


def test_a_model_claim_is_drawn_as_the_agents_and_never_as_the_platforms(
        db, owner, incident):
    """Through `investigate`, so this covers the real pipeline.

    What the graph guarantees about a root cause is attribution and
    traceability, NOT that the claim is any good. `drawn_by="agent"` is the
    whole point: a reader can tell a model's conclusion from a figure the
    calculation engine produced, and can walk from the conclusion to the
    evidence it cites.

    It deliberately does not assert the claim's content. `DeterministicProvider`
    currently labels its recovery-plan summary as a `root_cause` finding — see
    the note in `app/evidence/graph.py` — and a test asserting good content here
    would be grading the stand-in planner rather than the graph.
    """
    from app.incidents.manager import investigate

    out = investigate(db, incident, owner)
    assert out["task"] is not None

    causes = [e for e in edges_for(db, incident.id, Predicate.CAUSED_BY)
              if e.object_type == "root_cause"]
    for edge in causes:
        # Attributed to the agent, so nothing reads it as a platform figure.
        assert edge.drawn_by == "agent"
        # And walkable back to what it was concluded from.
        assert edge.object_value.get("evidence_refs")

    # The platform's own edges are attributed to the code that computed them,
    # and never to the agent.
    computed = [e for e in edges_for(db, incident.id, Predicate.CREATES)]
    assert computed and all(e.drawn_by == "calculation_engine" for e in computed)


def test_the_graph_records_one_root_cause_not_a_list_of_guesses(db, incident):
    build(db, incident, findings=[
        {"finding_type": "root_cause", "claim": "First guess.",
         "evidence_refs": ["TC_001"]},
        {"finding_type": "root_cause", "claim": "Second guess.",
         "evidence_refs": ["TC_002"]},
    ])
    causes = [e for e in edges_for(db, incident.id, Predicate.CAUSED_BY)
              if e.object_type == "root_cause"]
    assert len(causes) == 1


# ------------------------------------------------------------------ reading
def test_explain_groups_by_relationship_and_omits_empty_ones(db, incident):
    build(db, incident)
    out = explain(db, incident.id)

    assert "SUPPORTED_BY" in out
    assert "CREATES" in out
    # Nothing contradicts this incident, so there is no heading for it.
    assert "CONTRADICTS" not in out
    assert all(isinstance(v, list) and v for v in out.values())


def test_why_returns_one_line_per_edge_and_invents_nothing(db, incident):
    build(db, incident)
    lines = why(db, incident.id)
    assert len(lines) == len(edges_for(db, incident.id))
    assert all("--" in line and "[" in line for line in lines)


def test_the_graph_does_not_cross_incidents(db):
    detect(db, "MERCH_A")
    incidents = db.query(Incident).filter(Incident.merchant_id == "MERCH_A").all()
    assert len(incidents) >= 2

    build(db, incidents[0])
    build(db, incidents[1])

    a = {e.id for e in edges_for(db, incidents[0].id)}
    b = {e.id for e in edges_for(db, incidents[1].id)}
    assert a and b and a.isdisjoint(b)


def test_every_edge_carries_the_merchant_that_owns_it(db, incident):
    """The graph is a read surface, and read surfaces are scoped (§54, §57)."""
    build(db, incident)
    assert all(e.merchant_id == incident.merchant_id
               for e in edges_for(db, incident.id))
