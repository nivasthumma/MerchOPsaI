"""Detection engine against the seeded dataset — MerchantOps §12, §13.

The dataset plants a UPI degradation (`scripts/seed_data.py`) that the agent was
previously *told* about in a scenario prompt. These tests assert the system finds
it unaided.
"""
from __future__ import annotations

from app.detection import detect
from app.detection.rules import (
    DEGRADATION_THRESHOLD_PP, detect_payment_degradation, detect_duplicate_payments,
)
from app.models import (
    AuditLog, Incident, IncidentEvidence, IncidentSeverity, IncidentStatus, IncidentType,
)


def test_finds_the_planted_upi_degradation(db):
    found = detect_payment_degradation(db, "MERCH_A")
    upi = [a for a in found if a.signals["method"] == "upi"]
    assert len(upi) == 1, f"expected one UPI anomaly, got {[a.title for a in found]}"

    a = upi[0]
    s = a.signals
    assert s["baseline_success_rate_pct"] > s["current_success_rate_pct"]
    assert s["drop_pct_points"] >= DEGRADATION_THRESHOLD_PP
    assert a.incident_type is IncidentType.PAYMENT_DEGRADATION


def test_healthy_methods_do_not_trip_the_rule(db):
    """The rule must discriminate. Card, netbanking and wallet run at ~96.5% in
    both periods; if they also fired, the threshold would be measuring noise."""
    found = detect_payment_degradation(db, "MERCH_A")
    methods = {a.signals["method"] for a in found}
    assert methods == {"upi"}, f"non-degraded methods tripped the rule: {methods - {'upi'}}"


def test_revenue_at_risk_is_computed_not_asserted(db):
    """MerchantOps §22 — the calculation engine owns the number.

    Recomputed here straight from SQL rather than from the anomaly's own
    signals, so this is an independent check of the arithmetic and not a
    restatement of it.
    """
    from datetime import timedelta

    from sqlalchemy import text

    from app.detection.rules import PERIOD_DAYS
    from scripts.seed_data import ANCHOR

    cut = ANCHOR - timedelta(days=PERIOD_DAYS)
    prev = ANCHOR - timedelta(days=PERIOD_DAYS * 2)
    r = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE created_at >= :cut) AS cur_total,
               COUNT(*) FILTER (WHERE created_at >= :cut AND status <> 'failed') AS cur_ok,
               COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut) AS prev_total,
               COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut
                                  AND status <> 'failed') AS prev_ok,
               COALESCE(AVG(amount_minor) FILTER (WHERE created_at >= :cut
                                                    AND status <> 'failed'), 0) AS avg_ok
        FROM payments WHERE merchant_id = 'MERCH_A' AND method = 'upi'
    """), {"cut": cut, "prev": prev}).mappings().one()

    base_rate = int(r["prev_ok"]) / int(r["prev_total"])
    shortfall = int(r["cur_total"]) * base_rate - int(r["cur_ok"])
    assert shortfall > 0

    a = [x for x in detect_payment_degradation(db, "MERCH_A")
         if x.signals["method"] == "upi"][0]
    assert a.revenue_at_risk_minor == int(round(shortfall * int(r["avg_ok"])))


def test_published_signals_reproduce_the_figure(db):
    """An operator recomputing revenue-at-risk from the signals the incident
    displays must land on the figure it displays. Rates are published to one
    decimal place, so the agreement is to display precision rather than exact --
    but a financial number nobody can reproduce from its own evidence is a
    number that has to be taken on trust, which is what §22 is avoiding."""
    a = [x for x in detect_payment_degradation(db, "MERCH_A")
         if x.signals["method"] == "upi"][0]
    s = a.signals

    shortfall = s["current_attempts"] * (s["baseline_success_rate_pct"] / 100.0) \
        - s["actual_successes"]
    recomputed = round(shortfall * s["average_transaction_value_minor"])

    assert a.revenue_at_risk_minor > 0
    assert abs(recomputed - a.revenue_at_risk_minor) / a.revenue_at_risk_minor < 0.001


def test_onset_is_inside_the_planted_window(db):
    """seed_data plants the failures at hours 17-20. `started_at` must land in
    that window, not at the window boundary — §51's timeline is a claim about
    when the problem began."""
    a = [x for x in detect_payment_degradation(db, "MERCH_A")
         if x.signals["method"] == "upi"][0]
    assert 17 <= a.started_at.hour <= 20, a.started_at


def test_detection_is_idempotent(db):
    first = detect(db, "MERCH_A")
    assert first.incidents_created > 0
    assert first.already_known == 0

    second = detect(db, "MERCH_A")
    assert second.incidents_created == 0, "a second sweep manufactured new incidents"
    assert second.already_known == first.incidents_created
    assert second.anomalies_found == first.anomalies_found

    assert db.query(Incident).count() == first.incidents_created


def test_incident_carries_evidence_and_audit(db):
    rep = detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.PAYMENT_DEGRADATION).first()
    assert inc is not None
    assert inc.status is IncidentStatus.DETECTED
    assert inc.severity in set(IncidentSeverity)
    assert inc.detection_version == "detection-v1"

    ev = db.query(IncidentEvidence).filter(IncidentEvidence.incident_id == inc.id).all()
    keys = {e.key for e in ev}
    assert {"current_success_rate", "baseline_success_rate", "revenue_at_risk"} <= keys
    assert all(e.source in ("payments", "calculation_engine") for e in ev)

    audit = (db.query(AuditLog).filter(AuditLog.incident_id == inc.id)
             .order_by(AuditLog.id).all())
    assert [a.event_type for a in audit] == ["incident_detected"]
    assert audit[0].merchant_id == "MERCH_A"
    assert rep.duration_ms >= 0


def test_detection_is_merchant_scoped(db):
    detect(db, "MERCH_A")
    detect(db, "MERCH_B")
    for inc in db.query(Incident).all():
        key_merchant = inc.detection_key.split("|")[0]
        assert key_merchant == inc.merchant_id


def test_duplicate_payments_become_incidents(db):
    found = detect_duplicate_payments(db, "MERCH_A")
    assert found, "the seeded duplicate pair was not detected"
    a = found[0]
    assert a.incident_type is IncidentType.DUPLICATE_PAYMENT
    # The earliest capture is the real payment and is never listed as excess.
    assert a.signals["first_payment_id"] not in a.signals["excess_payment_ids"]
    excess = len(a.signals["excess_payment_ids"])
    assert a.revenue_at_risk_minor == (
        excess * a.signals["amount_minor"] - a.signals["already_refunded_minor"])


def test_one_incident_per_order_not_per_pair(db):
    """An order captured N times is ONE duplicate problem with N-1 excess
    charges. Emitting a pair at a time made a triple into three incidents each
    claiming the full amount, for 3x an exposure that is really 2x."""
    from sqlalchemy import text

    # Plant a third capture on the seeded duplicate order, inside the window.
    row = db.execute(text("""
        SELECT order_id, customer_id, amount_minor, method, created_at
        FROM payments WHERE id = 'SYN_PAY_0002'
    """)).mappings().one()
    db.execute(text("""
        INSERT INTO payments (id, merchant_id, order_id, customer_id, amount_minor,
                              currency, method, status, amount_refunded_minor, created_at)
        VALUES ('SYN_PAY_TRIPLE', 'MERCH_A', :o, :c, :a, 'INR', :m, 'captured', 0,
                :t + interval '60 seconds')
    """), {"o": row["order_id"], "c": row["customer_id"], "a": row["amount_minor"],
           "m": row["method"], "t": row["created_at"]})
    db.flush()

    found = [a for a in detect_duplicate_payments(db, "MERCH_A")
             if a.signals["order_id"] == row["order_id"]]
    assert len(found) == 1, f"one order produced {len(found)} incidents"
    a = found[0]
    assert a.signals["capture_count"] == 3
    assert len(a.signals["excess_payment_ids"]) == 2
    # Two excess captures, not three overlapping claims of one.
    assert a.revenue_at_risk_minor == 2 * int(row["amount_minor"])


# --------------------------------------------- multivariate detection (v2 §18)
def test_the_sweep_records_what_else_saw_the_same_episode(db):
    """v2 §18. Correlation happens over the whole sweep, not per rule."""
    report = detect(db, "MERCH_A")
    assert report.incidents_created > 0

    incidents = db.query(Incident).filter(Incident.merchant_id == "MERCH_A").all()
    for inc in incidents:
        correlation = (inc.signals or {}).get("correlation")
        assert correlation is not None, f"{inc.id} has no correlation facts"
        assert correlation["corroboration"] >= 1
        # A rule never corroborates itself.
        assert inc.detection_rule not in correlation["corroborating_rules"]
        assert correlation["multivariate"] == (correlation["corroboration"] > 1)


def test_corroboration_reaches_the_confidence_band(db, owner):
    """The two v2 corrections meet here: §18's signal feeds §33's band."""
    from app.incidents.manager import investigate

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(Incident.merchant_id == "MERCH_A").first()
    corroborating = len((inc.signals or {})["correlation"]["corroborating_rules"])

    investigate(db, inc, owner)
    assert inc.confidence_inputs["corroborating_rules"] == corroborating
