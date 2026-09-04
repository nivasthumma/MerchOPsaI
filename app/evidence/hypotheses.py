"""Competing hypotheses, tested against evidence — MerchantOps v2 §30.

§30's worked example, and the shape this module produces:

    H1  UPI provider degradation
    H2  Merchant configuration problem
    H3  Traffic anomaly
    H4  Customer-segment-specific problem

    Provider evidence:      supports H1
    Merchant configuration: normal
    Traffic:                normal
    Customer segment:       not concentrated

    Final: H1 = strongest supported explanation

## Why this is more than a list

The value in §30 is not that four sentences get written down. It is that each
one is **tested**, and that the test can fail. A hypothesis engine whose
hypotheses cannot be contradicted is a list of guesses with extra ceremony.

So every probe below runs a real query against real rows and returns a verdict
that could have gone the other way. `traffic_anomaly` is rejected here because
UPI attempt volume genuinely did not move; if the seed changed and volume
spiked, it would be supported instead, and `provider_degradation` would stop
being the sole survivor.

## What the platform owns

Adjudication. Hypotheses may be proposed by the template set below or added by
the model; which one *wins* is computed from supporting and contradicting
evidence, by `adjudicate`, deterministically. This is §33's rule applied to
explanations: the model reasons, the platform decides what the reasoning
established.

## UNTESTED is a real verdict

`merchant_configuration` has no probe, because this system stores no merchant
configuration to compare against. That is reported as UNTESTED rather than
quietly dropped or folded into REJECTED. A hypothesis nobody could test is a
gap in the platform's instrumentation, and hiding it behind a confident-looking
"rejected" is how the gap stays hidden. Same reasoning as §53's UNKNOWN and
§33's INSUFFICIENT.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.evidence.graph import draw
from app.models import (
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentType,
    Predicate,
)


@dataclass(frozen=True)
class Probe:
    """The outcome of testing one hypothesis against the data.

    `supports` and `contradicts` are separate rather than a single signed
    score. A probe that found nothing either way is not the midpoint between
    support and contradiction — it is a different answer, and a score collapses
    the two.
    """
    supports: bool
    contradicts: bool
    detail: str
    facts: dict


def _no_probe(detail: str) -> Probe:
    return Probe(False, False, detail, {})


# --------------------------------------------------------------------------
# The probes
# --------------------------------------------------------------------------
def _probe_provider_degradation(session, incident: Incident) -> Probe:
    """Do the failures carry one provider-side error, or many different ones?

    A single dominant error code across a method's failures is what a provider
    problem looks like from here. Failures scattered across unrelated codes look
    like something else — an expiring card cohort, a merchant-side validation
    change — and argue against this hypothesis rather than merely not supporting
    it.
    """
    method = (incident.signals or {}).get("method")
    if not method:
        return _no_probe("the incident names no payment method")

    rows = session.execute(text("""
        SELECT COALESCE(error_reason, 'unknown') AS reason, COUNT(*) AS n
        FROM payments
        WHERE merchant_id = :m AND method = :method AND status = 'failed'
        GROUP BY 1 ORDER BY 2 DESC
    """), {"m": incident.merchant_id, "method": method}).mappings().all()

    total = sum(r["n"] for r in rows)
    if not total:
        return _no_probe(f"no failed {method} payments to inspect")

    top, share = rows[0]["reason"], rows[0]["n"] / total
    facts = {"method": method, "dominant_error": top,
             "dominant_share": round(share, 3), "distinct_errors": len(rows),
             "failed_payments": total}

    # Corroboration from the provider's own event stream is the strongest form
    # this can take: it is the one signal that did not originate inside here.
    corroborating = ((incident.signals or {}).get("correlation") or {}) \
        .get("corroborating_rules", [])
    if "provider_failure_burst" in corroborating:
        facts["provider_reported_burst"] = True
        return Probe(True, False,
                     f"the provider itself reported a failure burst in this window, "
                     f"and {share:.0%} of failed {method} payments carry {top}", facts)

    if share >= 0.8:
        return Probe(True, False,
                     f"{share:.0%} of failed {method} payments carry a single "
                     f"provider error ({top})", facts)
    return Probe(False, True,
                 f"failures are spread across {len(rows)} different error reasons, "
                 f"which is not what one failing provider looks like", facts)


def _probe_traffic_anomaly(session, incident: Incident) -> Probe:
    """Did attempt volume actually move, or only the success rate?

    This is the hypothesis most worth testing, because it is the one a reader
    assumes away. If volume is flat and only conversion fell, traffic is not the
    explanation — and saying so is what makes the surviving hypothesis mean
    something.
    """
    method = (incident.signals or {}).get("method")
    if not method:
        return _no_probe("the incident names no payment method")

    row = session.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE created_at >= :start)                  AS during,
          COUNT(*) FILTER (WHERE created_at <  :start
                             AND created_at >= :start - (:span * INTERVAL '1 second'))
                                                                       AS before
        FROM payments
        WHERE merchant_id = :m AND method = :method
    """), {"m": incident.merchant_id, "method": method,
           "start": incident.started_at,
           "span": _window_seconds(incident)}).mappings().one()

    during, before = int(row["during"]), int(row["before"])
    facts = {"method": method, "attempts_during": during, "attempts_before": before}
    if before == 0:
        return _no_probe("no comparable earlier window to measure volume against")

    change = (during - before) / before
    facts["volume_change"] = round(change, 3)
    if abs(change) >= 0.5:
        return Probe(True, False,
                     f"{method} attempt volume moved {change:+.0%} against the "
                     f"preceding window", facts)
    return Probe(False, True,
                 f"{method} attempt volume moved only {change:+.0%}; the failures "
                 f"are not explained by a change in traffic", facts)


def _probe_customer_segment(session, incident: Incident) -> Probe:
    """Are the failures concentrated in a few customers, or spread across many?

    Concentration is what a segment-specific problem looks like. A failure count
    close to the number of distinct customers affected is the opposite: everyone
    hit once, which argues the cause is not about who the customers are.
    """
    method = (incident.signals or {}).get("method")
    if not method:
        return _no_probe("the incident names no payment method")

    row = session.execute(text("""
        SELECT COUNT(DISTINCT customer_id) AS customers, COUNT(*) AS failures
        FROM payments
        WHERE merchant_id = :m AND method = :method AND status = 'failed'
    """), {"m": incident.merchant_id, "method": method}).mappings().one()

    customers, failures = int(row["customers"]), int(row["failures"])
    if not failures:
        return _no_probe(f"no failed {method} payments to inspect")

    facts = {"affected_customers": customers, "failures": failures,
             "failures_per_customer": round(failures / max(customers, 1), 2)}
    # Few customers carrying many failures each is concentration.
    if customers and failures / customers >= 3.0:
        return Probe(True, False,
                     f"{failures} failures fall on only {customers} customers, "
                     f"which is concentrated", facts)
    return Probe(False, True,
                 f"{failures} failures are spread across {customers} customers, "
                 f"so this is not specific to a segment", facts)


def _probe_merchant_configuration(session, incident: Incident) -> Probe:
    """Untestable here, and said so rather than guessed.

    Testing it needs a record of the merchant's payment configuration and its
    change history. This system stores neither. Returning "no support" would
    read as a tested hypothesis that failed, which is a stronger claim than the
    truth and hides the instrumentation gap behind a verdict.
    """
    return _no_probe(
        "no merchant payment configuration or change history is recorded, "
        "so this cannot be tested either way")


def _window_seconds(incident: Incident) -> int:
    """How long the incident has been running, for a like-for-like comparison.

    Floored at an hour: a comparison window shorter than the detection rules'
    own bucket compares against noise.
    """
    now = datetime.now(UTC)
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(int((now - started).total_seconds()), 3600)


# --------------------------------------------------------------------------
# The template set
# --------------------------------------------------------------------------
# §30's four, for the incident type they were written about. Ordered, because
# the labels H1..H4 come from position and a merchant reading the console and a
# reviewer reading a trace must mean the same H2.
_FOR_DEGRADATION = (
    ("provider_degradation", "The payment provider degraded for this method.",
     _probe_provider_degradation),
    ("merchant_configuration", "A merchant configuration change broke this method.",
     _probe_merchant_configuration),
    ("traffic_anomaly", "A change in traffic volume explains the movement.",
     _probe_traffic_anomaly),
    ("customer_segment", "The problem is specific to a segment of customers.",
     _probe_customer_segment),
)

TEMPLATES: dict[IncidentType, tuple] = {
    IncidentType.PAYMENT_DEGRADATION: _FOR_DEGRADATION,
    # PROVIDER_FAILURE_BURST is the same question reached from the event store,
    # so it gets the same candidates.
    IncidentType.PROVIDER_FAILURE_BURST: _FOR_DEGRADATION,
}


def candidates_for(incident: Incident) -> tuple:
    """The template set for an incident type, or empty.

    Empty is a legitimate answer. A duplicate payment has one explanation and
    manufacturing three more to reject would be ceremony, not reasoning.
    """
    return TEMPLATES.get(incident.incident_type, ())


# --------------------------------------------------------------------------
# Proposing and adjudicating
# --------------------------------------------------------------------------
def propose(session, incident: Incident) -> list[Hypothesis]:
    """Create the candidate hypotheses for an incident, once each.

    Idempotent through `uq_hypothesis_once`: re-investigating re-tests the same
    candidates rather than accumulating another copy of each.
    """
    from sqlalchemy.exc import IntegrityError

    out: list[Hypothesis] = []
    for n, (key, statement, _) in enumerate(candidates_for(incident), start=1):
        existing = session.execute(
            select(Hypothesis).where(Hypothesis.incident_id == incident.id,
                                     Hypothesis.key == key)
        ).scalar_one_or_none()
        if existing is not None:
            out.append(existing)
            continue

        h = Hypothesis(
            id=f"HYP_{uuid.uuid4().hex[:10].upper()}",
            tenant_id=getattr(incident, "tenant_id", None),
            merchant_id=incident.merchant_id,
            incident_id=incident.id,
            label=f"H{n}", key=key, statement=statement,
            status=HypothesisStatus.UNTESTED,
        )
        sp = session.begin_nested()
        try:
            session.add(h)
            session.flush()
            sp.commit()
        except IntegrityError:          # a concurrent proposer won
            sp.rollback()
            h = session.execute(
                select(Hypothesis).where(Hypothesis.incident_id == incident.id,
                                         Hypothesis.key == key)).scalar_one()
        else:
            _publish(session, incident, "hypothesis.created", h)
        out.append(h)
    return out


def adjudicate(session, incident: Incident) -> list[Hypothesis]:
    """Test every hypothesis and settle which explanation the evidence allows.

    Counts are **recomputed** from the probes rather than incremented, so a
    second adjudication after more evidence arrives cannot leave the cached
    totals disagreeing with the edges they summarise.
    """
    hypotheses = propose(session, incident)
    if not hypotheses:
        return []

    probes = {key: probe for key, _, probe in candidates_for(incident)}
    results: dict[str, Probe] = {}

    for h in hypotheses:
        probe = probes.get(h.key)
        result = probe(session, incident) if probe else _no_probe(
            "proposed without a probe, so nothing here can test it")
        results[h.key] = result

        h.support_count = 1 if result.supports else 0
        h.contradiction_count = 1 if result.contradicts else 0
        h.verdict_reason = result.detail
        h.adjudicated_at = datetime.now(UTC)

        # The probe's finding is recorded in the graph, so the verdict is
        # walkable rather than merely stated. This is what CONTRADICTS was
        # added to `Predicate` for.
        if result.supports or result.contradicts:
            draw(session, incident,
                 subject_type="hypothesis", subject_id=h.id,
                 predicate=(Predicate.SUPPORTED_BY if result.supports
                            else Predicate.CONTRADICTS),
                 object_type="probe", object_id=h.key,
                 object_value={"detail": result.detail, **result.facts},
                 drawn_by="hypothesis_engine")

    # Status. Contradiction settles a hypothesis on its own -- evidence
    # arguing against it is a stronger statement than evidence merely not
    # arguing for it.
    supported = [h for h in hypotheses if h.support_count and not h.contradiction_count]
    for h in hypotheses:
        if h.contradiction_count:
            was = h.status
            h.status = HypothesisStatus.REJECTED
            if was is not HypothesisStatus.REJECTED:
                _publish(session, incident, "hypothesis.rejected", h)
        elif h.support_count:
            # SUPPORTED only when it is the sole survivor. Two explanations
            # that both fit are two explanations that both fit, and calling the
            # first one "the" cause is the single-shot answer §30 exists to
            # avoid.
            h.status = (HypothesisStatus.SUPPORTED if len(supported) == 1
                        else HypothesisStatus.CONTENDING)
        else:
            h.status = HypothesisStatus.UNTESTED

    session.flush()
    return hypotheses


def leading(session, incident_id: str) -> Hypothesis | None:
    """The sole surviving explanation, or None.

    None when nothing survived and None when several did. Both are honest
    answers and neither is "the first one".
    """
    rows = list(session.execute(
        select(Hypothesis).where(Hypothesis.incident_id == incident_id,
                                 Hypothesis.status == HypothesisStatus.SUPPORTED)
    ).scalars().all())
    return rows[0] if len(rows) == 1 else None


def for_incident(session, incident_id: str) -> list[Hypothesis]:
    return list(session.execute(
        select(Hypothesis).where(Hypothesis.incident_id == incident_id)
        .order_by(Hypothesis.label)).scalars().all())


def _publish(session, incident: Incident, event_type: str, h: Hypothesis) -> None:
    """Raise the v2 §62 frame. Never fatal, for the reason in app/audit/trace.py."""
    import logging

    from app.events.bus import publish

    try:
        publish(session, event_type,
                payload={"hypothesis_id": h.id, "label": h.label, "key": h.key,
                         "statement": h.statement, "status": h.status.value,
                         "reason": h.verdict_reason},
                merchant_id=incident.merchant_id,
                tenant_id=getattr(incident, "tenant_id", None),
                incident_id=incident.id,
                correlation_id=incident.correlation_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "stream frame not raised for %s", event_type, exc_info=True)
