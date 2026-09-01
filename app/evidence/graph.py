"""Drawing and reading the evidence graph — MerchantOps v2 §32.

§32's figure, and the shape this module produces:

    Incident
       ├── caused_by    → UPI degradation
       ├── affects      → 1,842 customers
       ├── affects      → 2,100 payment attempts
       ├── creates      → ₹4.72L revenue risk
       └── supported_by → E101, E102, E103, E104

## Who may draw an edge

Deterministic code, from state that already exists. Never the model.

The distinction matters because an edge is a claim. "This incident affects 1,842
customers" is a number, and §22 and §34 put numbers in the calculation engine.
The model is already allowed to *cite* evidence — `app/agent/output.py` refuses
a claim citing evidence that does not exist — and that is a different act from
asserting that one thing caused another.

The one edge that comes closest to model territory is `CAUSED_BY`, and it is
drawn from the model's root-cause finding. What makes that safe is that the
finding is already grounded: it must cite evidence ids the runtime issued, or
the task fails with `AGENT_GROUNDING_FAILURE`. So the edge records a conclusion
the model reached *and* the evidence it reached it from, and a reader can walk
from one to the other. An ungrounded root cause never gets an edge because it
never becomes a finding.

## Idempotency

`draw` is safe to call repeatedly. The unique constraint on
(incident, subject, predicate, object) is the authority and a collision is the
check — the same mechanism `incidents.detection_key` uses, applied to
assertions rather than to observations. Re-investigating an incident redraws
its graph and does not double it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    EvidenceEdge, Incident, IncidentEvidence, Predicate, RecoveryCandidate,
)


def draw(session, incident: Incident, *, predicate: Predicate,
         object_type: str, object_id: str | None = None,
         object_value: dict | None = None, drawn_by: str,
         subject_type: str = "incident",
         subject_id: str | None = None) -> EvidenceEdge | None:
    """Assert one relationship. Returns None if it was already asserted.

    Written inside the caller's transaction, like everything else that
    describes state the caller is producing.
    """
    edge = EvidenceEdge(
        id=f"EDG_{uuid.uuid4().hex[:10].upper()}",
        tenant_id=getattr(incident, "tenant_id", None),
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        subject_type=subject_type,
        subject_id=subject_id or incident.id,
        predicate=predicate,
        object_type=object_type,
        object_id=object_id,
        object_value=object_value,
        drawn_by=drawn_by,
    )
    # SAVEPOINT, not a bare flush: a duplicate edge must discard this one
    # INSERT, not the edges already drawn in this pass.
    sp = session.begin_nested()
    try:
        session.add(edge)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        return None
    return edge


def edges_for(session, incident_id: str,
              predicate: Predicate | None = None) -> list[EvidenceEdge]:
    q = select(EvidenceEdge).where(EvidenceEdge.incident_id == incident_id)
    if predicate is not None:
        q = q.where(EvidenceEdge.predicate == predicate)
    return list(session.execute(
        q.order_by(EvidenceEdge.predicate, EvidenceEdge.created_at,
                   EvidenceEdge.id)).scalars().all())


def build(session, incident: Incident, *, findings: list | None = None) -> int:
    """Draw the graph for one incident from what is already recorded.

    Returns the number of NEW edges. Callable at any point in an incident's
    life: it draws whatever is knowable now, and drawing again after more
    becomes knowable adds only what is new.

    Order follows §32's figure, which is also roughly the order the facts
    become available.
    """
    drawn = 0

    # supported_by → every piece of evidence collected for this incident.
    # Untrusted rows are included and MARKED. Leaving them out would hide what
    # the system looked at; presenting them unmarked would let order free text
    # read as corroboration (MerchantOps §39, and see app/agent/confidence.py,
    # which counts them and never lets them support).
    for ev in session.execute(
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == incident.id)
        .order_by(IncidentEvidence.id)
    ).scalars().all():
        if draw(session, incident, predicate=Predicate.SUPPORTED_BY,
                object_type="evidence", object_id=ev.id,
                object_value={"key": ev.key, "source": ev.source,
                              "untrusted": ev.untrusted},
                drawn_by=incident.detection_rule):
            drawn += 1

    # creates → the exposure. The figure the calculation engine produced, not a
    # restatement of it: the edge points at the incident's own column.
    if incident.revenue_at_risk_minor:
        if draw(session, incident, predicate=Predicate.CREATES,
                object_type="revenue_at_risk",
                object_value={"amount_minor": incident.revenue_at_risk_minor,
                              "currency": "INR"},
                drawn_by="calculation_engine"):
            drawn += 1

    # caused_by → what the detection rule observed, plus what any other rule
    # saw of the same episode (v2 §18). Both are observations rather than
    # explanations, which is why they are separate from the model's root cause
    # below and attributed to the rule that made them.
    signals = incident.signals or {}
    if signals.get("method"):
        if draw(session, incident, predicate=Predicate.CAUSED_BY,
                object_type="payment_method", object_id=str(signals["method"]),
                object_value={"detection_rule": incident.detection_rule},
                drawn_by=incident.detection_rule):
            drawn += 1
    for rule in (signals.get("correlation") or {}).get("corroborating_rules", []):
        if draw(session, incident, predicate=Predicate.SUPPORTED_BY,
                object_type="detection_rule", object_id=rule,
                object_value={"multivariate": True},
                drawn_by="correlation_engine"):
            drawn += 1

    # affects → who and what. Drawn from recovery candidates, which is the only
    # place the system actually knows the answer: a candidate exists because a
    # specific payment for a specific customer was caught by this incident.
    candidates = _candidates_for(session, incident)
    if candidates:
        customers = {c.customer_id for c in candidates if c.customer_id}
        if customers and draw(session, incident, predicate=Predicate.AFFECTS,
                              object_type="customers",
                              object_value={"count": len(customers)},
                              drawn_by="recovery_planner"):
            drawn += 1
        if draw(session, incident, predicate=Predicate.AFFECTS,
                object_type="payments",
                object_value={"count": len(candidates)},
                drawn_by="recovery_planner"):
            drawn += 1

    # caused_by → the model's root cause, and only if it is grounded. An
    # ungrounded claim never becomes a finding, so reaching here means the
    # runtime already resolved its evidence ids.
    #
    # Selected on `finding_type == "root_cause"`, which `app/agent/output.py`
    # preserves from the model's own output, and NOT on `kind == "INFERRED"`.
    # INFERRED is the storage kind for three different model types --
    # root_cause, inference and uncertainty -- and the recovery planner's own
    # summary is INFERRED too, so filtering on it drew edges from findings that
    # were never claimed to be causes at all.
    #
    # KNOWN DEFECT, upstream of here: `DeterministicProvider` builds its
    # root_cause claim from the first sentence of whatever prose it produced,
    # and for an incident investigation that prose is the recovery plan. So it
    # emits `finding_type: "root_cause"` with the claim "intervention
    # PAYMENT_LINK, 32 of 33 candidates eligible" -- a plan, not a cause.
    #
    # The graph records it faithfully and attributes it to `agent`, which is
    # the honest thing to do with a claim the model made: a reader can tell it
    # from a figure the calculation engine produced and can walk to the
    # evidence it cites. Correcting the claim is a change to the planner's
    # semantics, and `task.findings` has carried this since long before the
    # graph existed -- the graph only made it visible, which is what §32 is
    # for. Not fixed here because it is not this module's to fix.
    for f in (findings or []):
        if f.get("finding_type") != "root_cause" or not f.get("claim"):
            continue
        if not f.get("evidence_refs"):
            # A conclusion citing nothing is a conclusion the graph cannot let
            # a reader walk back from, which is the one thing §32 is for.
            continue
        if draw(session, incident, predicate=Predicate.CAUSED_BY,
                object_type="root_cause", object_id=None,
                object_value={"claim": f["claim"][:500],
                              "evidence_refs": f.get("evidence_refs", [])},
                drawn_by="agent"):
            drawn += 1
            break        # the graph records one root cause, not a list of guesses

    return drawn


def _candidates_for(session, incident: Incident) -> list[RecoveryCandidate]:
    from app.models import RecoveryPlan

    return list(session.execute(
        select(RecoveryCandidate)
        .join(RecoveryPlan, RecoveryCandidate.plan_id == RecoveryPlan.id)
        .where(RecoveryPlan.incident_id == incident.id)
    ).scalars().all())


def explain(session, incident_id: str) -> dict:
    """§32's answer to "why do you believe this?", grouped by relationship.

    Returned as data rather than prose. The sentence a merchant reads is the
    UI's job; what the platform owes is the structure underneath it, so that
    the sentence can be checked against something.
    """
    out: dict[str, list[dict]] = {p.value: [] for p in Predicate}
    for e in edges_for(session, incident_id):
        out[e.predicate.value].append({
            "id": e.id,
            "subject": {"type": e.subject_type, "id": e.subject_id},
            "object": {"type": e.object_type, "id": e.object_id,
                       "value": e.object_value},
            "drawn_by": e.drawn_by,
            "at": e.created_at.isoformat(),
        })
    return {k: v for k, v in out.items() if v}


def why(session, incident_id: str) -> list[str]:
    """The same graph as short lines, for a log or a terminal.

    Deliberately not a narrative. Each line is one edge, so a reader can see
    that nothing was added between the graph and the explanation.
    """
    lines = []
    for e in edges_for(session, incident_id):
        target = e.object_id or ""
        if e.object_value and not target:
            target = ", ".join(f"{k}={v}" for k, v in e.object_value.items()
                               if k != "evidence_refs")
        lines.append(f"{e.subject_id} --{e.predicate.value.lower()}--> "
                     f"{e.object_type}({target}) [{e.drawn_by}]")
    return lines
