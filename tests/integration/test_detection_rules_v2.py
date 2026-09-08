"""The two detection rules MerchantOps §12 names and this build lacked.

    failure-code spikes        why a method is degrading
    unusual refund activity    money going back out

The most important test here is not that either rule fires. It is
`test_a_failure_spike_adds_no_revenue_to_the_ledger`: the spike explains
exposure the degradation rule has already counted, and counting it twice would
roughly double the at-risk figure on the Command Center — the one screen where
being wrong about money matters most.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import text

from app.detection.rules import (
    FAILURE_SPIKE_RATIO,
    MIN_FAILURE_VOLUME,
    MIN_REFUND_VOLUME,
    PERIOD_DAYS,
    REFUND_SPIKE_RATIO,
    detect_failure_code_spike,
    detect_unusual_refund_activity,
)
from scripts.seed_data import ANCHOR

CUT = ANCHOR - timedelta(days=PERIOD_DAYS)
PREV = ANCHOR - timedelta(days=PERIOD_DAYS * 2)


# --------------------------------------------------------- failure-code spike
def test_the_seeded_upi_collapse_is_explained_by_its_failure_code(db):
    """The planted degradation is collect timeouts, and the rule says so. The
    degradation incident reports that UPI fell; this reports why."""
    found = detect_failure_code_spike(db, "MERCH_A")
    reasons = {a.signals["error_reason"]: a for a in found}
    assert "UPI_COLLECT_TIMEOUT" in reasons, list(reasons)

    a = reasons["UPI_COLLECT_TIMEOUT"]
    assert a.signals["dominant_method"] == "upi"
    assert a.signals["observed"] > a.signals["baseline"]
    assert a.signals["ratio"] >= FAILURE_SPIKE_RATIO


def test_a_failure_spike_adds_no_revenue_to_the_ledger(db):
    """The load-bearing property.

    `build_ledger` sums `revenue_at_risk_minor` over open incidents. The lost
    revenue from a UPI collapse belongs to the degradation incident; attributing
    it here as well would show the merchant roughly twice their real exposure.
    """
    for a in detect_failure_code_spike(db, "MERCH_A"):
        assert a.revenue_at_risk_minor == 0, a.title
        # And the reason is on the record, not only in a code comment, so an
        # operator seeing a zero does not read it as "no impact".
        assert "revenue_note" in a.signals
        assert "degradation" in a.summary


def test_running_detection_does_not_inflate_at_risk(db, owner):
    """End to end: the ledger's at-risk figure is the same with the new rules
    running as it was with only the degradation rule."""
    from app.detection.engine import detect
    from app.detection.rules import detect_payment_degradation
    from app.recovery.ledger import build_ledger

    degradation_only = sum(a.revenue_at_risk_minor
                           for a in detect_payment_degradation(db, "MERCH_A"))
    detect(db, "MERCH_A")
    db.flush()

    led = build_ledger(db, "MERCH_A")
    # Duplicate-payment incidents carry their own genuine exposure, so at-risk
    # is degradation + duplicates and never more. What it must not include is a
    # second copy of the degradation figure.
    assert led.at_risk_minor >= degradation_only
    assert led.at_risk_minor < 2 * degradation_only, (
        "at-risk looks doubled; a diagnostic rule is claiming revenue")


def test_a_low_volume_reason_is_not_a_spike(db):
    """Three failures becoming six is a doubling and is noise."""
    _clear_reasons(db)
    _plant_failures(db, "RARE_DECLINE", cur=MIN_FAILURE_VOLUME - 1, prev=1)
    assert detect_failure_code_spike(db, "MERCH_A") == []

    # One more occurrence and it clears the floor.
    _plant_failures(db, "RARE_DECLINE", cur=1, prev=0)
    assert [a.signals["error_reason"]
            for a in detect_failure_code_spike(db, "MERCH_A")] == ["RARE_DECLINE"]


def test_a_reason_with_no_baseline_is_a_spike_not_a_division_by_zero(db):
    _clear_reasons(db)
    _plant_failures(db, "BRAND_NEW_ERROR", cur=MIN_FAILURE_VOLUME + 5, prev=0)

    found = detect_failure_code_spike(db, "MERCH_A")
    assert len(found) == 1
    assert found[0].signals["baseline"] == 0
    assert found[0].signals["ratio"] == MIN_FAILURE_VOLUME + 5


def test_a_steady_reason_does_not_fire(db):
    """`GATEWAY_DECLINED` runs flat across both windows in the seed. A rule that
    fires on it is a rule that fires on everything."""
    found = {a.signals["error_reason"] for a in detect_failure_code_spike(db, "MERCH_A")}
    assert "GATEWAY_DECLINED" not in found


def test_the_spike_rule_is_merchant_scoped(db):
    for a in detect_failure_code_spike(db, "MERCH_B"):
        assert "MERCH_B" in a.detection_key


def test_the_spike_is_idempotent_over_the_same_window(db, owner):
    """Re-running the sweep must not manufacture a second incident."""
    from app.detection.engine import detect

    first = detect(db, "MERCH_A")
    db.flush()
    second = detect(db, "MERCH_A")
    db.flush()
    assert second.incidents_created == 0
    assert second.already_known >= first.incidents_created


# ------------------------------------------------------ unusual refund activity
def test_the_seed_has_no_refund_anomaly_and_the_rule_says_nothing(db):
    """Honest about what the dataset can reach. The seed refunds nine payments
    in each window at similar value, so a rule that fired here would be firing
    on ordinary business."""
    assert detect_unusual_refund_activity(db, "MERCH_A") == []


def test_a_value_spike_is_detected_and_counted_as_excess_only(db):
    """Reporting the whole refunded total would describe ordinary business as an
    incident. The figure is the excess over baseline."""
    baseline_value = _refund_value(db, PREV, CUT)
    assert baseline_value > 0, "the seed must have a baseline to be unusual against"

    # Triple this window's outflow.
    _plant_refunds(db, count=MIN_REFUND_VOLUME, each_minor=baseline_value // 2)

    found = detect_unusual_refund_activity(db, "MERCH_A")
    assert len(found) == 1
    a = found[0]
    current = _refund_value(db, CUT, ANCHOR)
    assert a.revenue_at_risk_minor == current - baseline_value
    assert a.revenue_at_risk_minor < current, "the total is not the anomaly"
    assert a.signals["driver"] in ("value", "count")


def test_a_first_week_of_refunds_is_not_an_anomaly(db):
    """Otherwise every new account is greeted with an incident."""
    db.execute(text("DELETE FROM refunds WHERE merchant_id = 'MERCH_A'"))
    db.flush()
    _plant_refunds(db, count=MIN_REFUND_VOLUME * 3, each_minor=100_00)

    assert detect_unusual_refund_activity(db, "MERCH_A") == []


def test_a_count_spike_with_falling_value_claims_no_exposure(db):
    """Fifty small refunds where there are usually two is real and worth
    surfacing. Its excess VALUE is negative, which is not an amount at risk."""
    db.execute(text("DELETE FROM refunds WHERE merchant_id = 'MERCH_A' "
                    "AND created_at >= :c"), {"c": CUT})
    db.flush()
    baseline_n = _refund_count(db, PREV, CUT)
    assert baseline_n > 0

    # Many refunds, each tiny: count far up, value down.
    _plant_refunds(db, count=baseline_n * 4, each_minor=1_00)

    found = detect_unusual_refund_activity(db, "MERCH_A")
    assert len(found) == 1
    assert found[0].signals["driver"] == "count"
    assert found[0].revenue_at_risk_minor == 0


def test_below_the_floor_nothing_fires(db):
    db.execute(text("DELETE FROM refunds WHERE merchant_id = 'MERCH_A' "
                    "AND created_at >= :c"), {"c": CUT})
    db.flush()
    _plant_refunds(db, count=MIN_REFUND_VOLUME - 1, each_minor=500_00)
    assert detect_unusual_refund_activity(db, "MERCH_A") == []


def test_the_ratio_is_the_threshold_not_any_increase(db):
    """A refund week 20% up on the last one is a week, not an incident."""
    db.execute(text("DELETE FROM refunds WHERE merchant_id = 'MERCH_A' "
                    "AND created_at >= :c"), {"c": CUT})
    db.flush()
    baseline_v = _refund_value(db, PREV, CUT)
    baseline_n = _refund_count(db, PREV, CUT)

    # Just under the ratio on both count and value.
    under = int(baseline_v * (REFUND_SPIKE_RATIO - 0.5))
    _plant_refunds(db, count=baseline_n, each_minor=under // max(baseline_n, 1))
    assert detect_unusual_refund_activity(db, "MERCH_A") == []


# ------------------------------------------------------------------- helpers
def _clear_reasons(db) -> None:
    """Blank every seeded failure reason so a planted one stands alone.

    NULLing the column rather than deleting the rows: `provider_mappings`
    references payments, and one of the seeded failures is a mapped edge case
    (`SYN_PAY_0008`, the never-refundable capture). Deleting it fights a foreign
    key that is doing its job, and the rule reads `error_reason IS NOT NULL`
    anyway — so this removes the rows from the rule's view without removing
    them from the database.
    """
    db.execute(text("UPDATE payments SET error_reason = NULL "
                    "WHERE merchant_id = 'MERCH_A'"))
    db.flush()


def _plant_failures(db, reason: str, *, cur: int, prev: int) -> None:
    """Failed payments carrying one reason, in each window.

    Ids are random rather than derived from the reason and index, so a test may
    call this twice — to cross a threshold by one, say — without the second
    call colliding with the first on the primary key.

    Written through real rows rather than mocked queries: the rule is SQL, and a
    test that stubs the query tests the stub.
    """
    order_id, customer_id, product_id = db.execute(text(
        "SELECT order_id, customer_id, (SELECT id FROM products "
        " WHERE merchant_id = 'MERCH_A' LIMIT 1) "
        "FROM payments WHERE merchant_id = 'MERCH_A' LIMIT 1")).one()
    for n, base in ((cur, CUT + timedelta(hours=1)),
                    (prev, PREV + timedelta(hours=1))):
        for i in range(n):
            db.execute(text("""
                INSERT INTO payments (id, merchant_id, order_id, customer_id,
                    amount_minor, currency, method, status, error_reason,
                    amount_refunded_minor, created_at)
                VALUES (:id, 'MERCH_A', :o, :c, 100000, 'INR', 'upi', 'failed',
                        :r, 0, :at)
            """), {"id": f"SYN_PAY_T{uuid.uuid4().hex[:16].upper()}",
                   "o": order_id, "c": customer_id, "r": reason,
                   "at": base + timedelta(minutes=i)})
    db.flush()


def _plant_refunds(db, *, count: int, each_minor: int) -> None:
    payment_id = db.execute(text(
        "SELECT id FROM payments WHERE merchant_id = 'MERCH_A' LIMIT 1")).scalar()
    for i in range(count):
        db.execute(text("""
            INSERT INTO refunds (id, merchant_id, payment_id, amount_minor,
                                 status, created_at)
            VALUES (:id, 'MERCH_A', :p, :amt, 'processed', :at)
        """), {"id": f"SYN_RFN_T{uuid.uuid4().hex[:16].upper()}", "p": payment_id,
               "amt": each_minor, "at": CUT + timedelta(hours=1, minutes=i)})
    db.flush()


def _refund_value(db, start, end) -> int:
    return int(db.execute(text(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM refunds "
        "WHERE merchant_id = 'MERCH_A' AND status = 'processed' "
        "  AND created_at >= :s AND created_at < :e"),
        {"s": start, "e": end}).scalar() or 0)


def _refund_count(db, start, end) -> int:
    return int(db.execute(text(
        "SELECT COUNT(*) FROM refunds WHERE merchant_id = 'MERCH_A' "
        "  AND status = 'processed' AND created_at >= :s AND created_at < :e"),
        {"s": start, "e": end}).scalar() or 0)


# ------------------------------------------------- the new types downstream
def test_every_incident_type_has_a_declared_intervention():
    """A new incident type inheriting the `.get()` fallback is a remedy nobody
    chose, and for a financial action that is the wrong provenance. Importing
    the planner already raises on a gap; this states why in a place a reader
    looking for the rule will find it."""
    from app.models import IncidentType
    from app.recovery.planner import _INTERVENTION

    assert set(_INTERVENTION) == set(IncidentType)


def test_the_new_types_are_never_remedied_automatically(db):
    """`UNUSUAL_REFUND_ACTIVITY` is the one worth reading twice: every payment
    underneath it SUCCEEDED. Refunding more compounds the problem and reversing
    a refund is not an operation this system has."""
    from app.models import IncidentType, Intervention
    from app.recovery.planner import _INTERVENTION

    for t in (IncidentType.FAILURE_CODE_SPIKE,
              IncidentType.UNUSUAL_REFUND_ACTIVITY):
        assert _INTERVENTION[t] is Intervention.HUMAN_ESCALATION


def test_planning_a_diagnostic_incident_proposes_nothing(db, owner):
    """It must not crash, and it must not propose payment links for the same
    transactions the degradation incident already covers."""
    from app.detection.engine import detect
    from app.models import Incident, IncidentType, Intervention
    from app.recovery.planner import plan_recovery

    detect(db, "MERCH_A")
    db.flush()
    spike = (db.query(Incident)
             .filter(Incident.merchant_id == "MERCH_A",
                     Incident.incident_type == IncidentType.FAILURE_CODE_SPIKE)
             .one_or_none())
    assert spike is not None, "the seeded UPI collapse should raise a spike"

    result = plan_recovery(db, spike, principal=owner)
    plan = getattr(result, "plan", result)
    assert plan.intervention is Intervention.HUMAN_ESCALATION
    assert plan.eligible_recovery_minor == 0
    assert plan.expected_recovery_minor == 0
