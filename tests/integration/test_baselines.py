"""Adaptive baselines — MerchantOps v2 §17.

§17's purpose is one sentence: "This prevents normal traffic patterns from
becoming false incidents." So the test that carries this file is the one where
the flat rule WOULD fire and the seasonal baseline stops it — and, just as
importantly, the one where the seasonal baseline is asked to stop a real
incident and refuses.

The rows are built here rather than carved out of the seeded dataset. The shape
needed is a specific statistical one — same per-hour rates, different hour mix
between two windows — and reshaping 600 rows of realistic traffic into it is
both harder to get right and harder to read than stating it directly.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.detection.baselines import (
    MIN_COVERAGE, MIN_SLOT_SAMPLES, explains_drop, seasonal_baseline,
)
from app.detection.rules import (
    DEGRADATION_THRESHOLD_PP, MIN_VOLUME, PERIOD_DAYS,
    detect_payment_degradation,
)
from scripts.seed_data import ANCHOR

CUT = ANCHOR - timedelta(days=PERIOD_DAYS)
METHOD = "testrail"          # its own method, so it cannot disturb the seeded ones


def _rows(db, *, day_offset: int, hour: int, ok: int, failed: int):
    """Insert payments at an exact UTC hour, reusing a seeded order/customer.

    `created_at` is built in UTC explicitly. Letting the database interpret a
    naive timestamp in the session zone is the bug this module already carries a
    comment about, and a fixture that made it would silently test different
    hours than the code under test.
    """
    anchor = db.execute(text("""
        SELECT id, order_id, customer_id, amount_minor, currency
        FROM payments WHERE merchant_id = 'MERCH_A' LIMIT 1
    """)).mappings().one()

    when = (ANCHOR - timedelta(days=day_offset)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    for i in range(ok + failed):
        db.execute(text("""
            INSERT INTO payments (id, merchant_id, order_id, customer_id,
                                  amount_minor, currency, method, status,
                                  error_reason, amount_refunded_minor, created_at)
            VALUES (:id, 'MERCH_A', :order_id, :customer_id, :amount, :currency,
                    :method, :status, :reason, 0, :when)
        """), {"id": f"TB_{day_offset}_{hour}_{i}", "order_id": anchor["order_id"],
               "customer_id": anchor["customer_id"], "amount": anchor["amount_minor"],
               "currency": anchor["currency"], "method": METHOD,
               "status": "captured" if i < ok else "failed",
               "reason": None if i < ok else "RAIL_TIMEOUT", "when": when})
    db.flush()


def _stable_pattern(db, *, quiet_hour_ok, quiet_hour_bad, busy_hour_ok,
                    busy_hour_bad, days, offset_from):
    """The same hourly shape on every day in a window: hour 9 good, hour 18 bad."""
    for d in range(days):
        _rows(db, day_offset=offset_from - d, hour=9,
              ok=quiet_hour_ok, failed=quiet_hour_bad)
        _rows(db, day_offset=offset_from - d, hour=18,
              ok=busy_hour_ok, failed=busy_hour_bad)


# ------------------------------------------------------- the §17 case
def test_a_recurring_pattern_does_not_become_an_incident(db):
    """The case §17 exists for.

    Hour 18 always converts badly. Both windows have the same per-hour rates;
    the current one simply carries more hour-18 traffic. The flat rule compares
    aggregates, sees a fall, and would open an incident every week for having
    evenings. The seasonal baseline recognises the hours.
    """
    # Previous week: mostly the good hour.
    _stable_pattern(db, quiet_hour_ok=19, quiet_hour_bad=1,
                    busy_hour_ok=2, busy_hour_bad=8,
                    days=7, offset_from=14)
    # Current week: same rates per hour, weighted towards the bad one.
    _stable_pattern(db, quiet_hour_ok=4, quiet_hour_bad=1,
                    busy_hour_ok=4, busy_hour_bad=16,
                    days=7, offset_from=7)

    rates = db.execute(text("""
        SELECT
          100.0 * COUNT(*) FILTER (WHERE created_at >= :cut AND status <> 'failed')
                / NULLIF(COUNT(*) FILTER (WHERE created_at >= :cut), 0) AS cur,
          100.0 * COUNT(*) FILTER (WHERE created_at < :cut AND status <> 'failed')
                / NULLIF(COUNT(*) FILTER (WHERE created_at < :cut), 0) AS prev
        FROM payments WHERE merchant_id = 'MERCH_A' AND method = :method
    """), {"cut": CUT, "method": METHOD}).one()

    # The flat rule's premise holds: week on week, this looks like a fall.
    assert rates.prev - rates.cur >= DEGRADATION_THRESHOLD_PP, (
        "the fixture does not reproduce the false positive it exists to prevent")

    baseline = seasonal_baseline(db, "MERCH_A", METHOD,
                                 window_start=CUT, history_days=PERIOD_DAYS)
    assert baseline.measured, f"no seasonal opinion: {baseline.as_dict()}"
    assert explains_drop(baseline, rates.cur, DEGRADATION_THRESHOLD_PP), (
        f"the seasonal baseline did not recognise its own hours: "
        f"expected {baseline.expected_rate:.1f}%, current {rates.cur:.1f}%")

    # And end to end: no incident for this method.
    assert METHOD not in {a.signals["method"]
                          for a in detect_payment_degradation(db, "MERCH_A")}


def test_a_real_degradation_is_not_suppressed(db):
    """The other half, and the one that matters more.

    Same hours, but this week they perform far worse than they ever have. The
    seasonal baseline is asked to explain it and cannot.
    """
    _stable_pattern(db, quiet_hour_ok=19, quiet_hour_bad=1,
                    busy_hour_ok=18, busy_hour_bad=2,
                    days=7, offset_from=14)
    # Same mix as before, and now the same hours are failing.
    _stable_pattern(db, quiet_hour_ok=10, quiet_hour_bad=10,
                    busy_hour_ok=6, busy_hour_bad=14,
                    days=7, offset_from=7)

    found = {a.signals["method"]: a
             for a in detect_payment_degradation(db, "MERCH_A")}
    assert METHOD in found, "a genuine degradation was suppressed"
    # And the incident records which baseline had an opinion about it.
    seasonal = found[METHOD].signals["seasonal_baseline"]
    assert seasonal["granularity"] in ("DOW_HOUR", "HOUR")
    assert seasonal["expected_rate_pct"] is not None


# ------------------------------------------------- sufficiency, not guesswork
def test_no_history_means_no_opinion_and_no_veto(db):
    """FLAT is not agreement. A baseline with nothing behind it must not
    suppress anything."""
    _stable_pattern(db, quiet_hour_ok=20, quiet_hour_bad=0,
                    busy_hour_ok=5, busy_hour_bad=15,
                    days=7, offset_from=7)          # current window only

    baseline = seasonal_baseline(db, "MERCH_A", METHOD,
                                 window_start=CUT, history_days=PERIOD_DAYS)
    assert baseline.granularity == "FLAT"
    assert baseline.measured is False
    assert baseline.expected_rate is None
    # Even at a rate it would happily have explained with history behind it.
    assert explains_drop(baseline, 50.0, DEGRADATION_THRESHOLD_PP) is False


def test_a_thin_slot_does_not_earn_a_veto(db):
    """Two readings of an hour are two readings, not a baseline.

    The count here is **hardcoded**, not derived from `MIN_SLOT_SAMPLES`. An
    earlier version wrote `range(MIN_SLOT_SAMPLES - 1)`, which moves with the
    constant: lowering the guard to 1 also lowered the fixture to zero rows and
    the assertion went on passing. A mutation run caught it —
    `MIN_SLOT_SAMPLES = 1` SURVIVED.

    A test parameterised by the constant it is testing cannot detect a change
    to that constant. It reads as more rigorous than a literal and is worth
    strictly less.
    """
    assert MIN_SLOT_SAMPLES > 2, (
        "this fixture plants 2 observations to sit below the guard; if the "
        "guard drops to 2 the fixture has to change with intent, not silently")

    for d in range(2):
        _rows(db, day_offset=14 - d, hour=18, ok=2, failed=8)
    _stable_pattern(db, quiet_hour_ok=4, quiet_hour_bad=1,
                    busy_hour_ok=4, busy_hour_bad=16,
                    days=7, offset_from=7)

    baseline = seasonal_baseline(db, "MERCH_A", METHOD,
                                 window_start=CUT, history_days=PERIOD_DAYS)
    assert baseline.granularity == "FLAT", (
        f"2 observations bought an opinion: {baseline.as_dict()}")


def test_partial_history_is_not_stretched_over_the_gaps(db):
    """Enough history to be tempting is not enough to suppress an incident.

    The current window trades mostly in an hour nothing has been seen in. The
    covered slice would produce a confident-looking expectation drawn from a
    minority of the volume, and extrapolating it across the rest is guesswork —
    which must not veto.
    """
    # History for hour 9 only.
    for d in range(7):
        _rows(db, day_offset=14 - d, hour=9, ok=18, failed=2)
    # Current window trades mostly at hour 15, which has no history at all.
    for d in range(7):
        _rows(db, day_offset=7 - d, hour=9, ok=2, failed=0)
        _rows(db, day_offset=7 - d, hour=15, ok=3, failed=12)

    baseline = seasonal_baseline(db, "MERCH_A", METHOD,
                                 window_start=CUT, history_days=PERIOD_DAYS)
    assert baseline.coverage < MIN_COVERAGE or baseline.granularity == "FLAT"
    assert baseline.granularity == "FLAT", (
        f"a minority of covered volume bought an opinion: {baseline.as_dict()}")
    assert explains_drop(baseline, 20.0, DEGRADATION_THRESHOLD_PP) is False


def test_the_baseline_reports_what_it_was_computed_from(db):
    """A suppression nobody can attribute to evidence is one nobody can argue
    with, which for a control that HIDES incidents is the wrong way round."""
    _stable_pattern(db, quiet_hour_ok=19, quiet_hour_bad=1,
                    busy_hour_ok=2, busy_hour_bad=8,
                    days=7, offset_from=14)
    _stable_pattern(db, quiet_hour_ok=4, quiet_hour_bad=1,
                    busy_hour_ok=4, busy_hour_bad=16,
                    days=7, offset_from=7)

    d = seasonal_baseline(db, "MERCH_A", METHOD,
                          window_start=CUT, history_days=PERIOD_DAYS).as_dict()
    for key in ("granularity", "expected_rate_pct", "coverage", "slots",
                "samples", "min_slot_samples"):
        assert key in d, key
    assert d["coverage"] >= MIN_COVERAGE
    assert d["slots"] > 0 and d["samples"] > 0


# ------------------------------------------------------------- timezone
def test_the_baseline_does_not_move_with_the_servers_timezone(db):
    """`EXTRACT(HOUR FROM created_at)` on a timestamptz renders in the SESSION
    zone, so the same rows bucket into different hours on differently
    configured servers. `app/detection/baselines.py` casts to UTC first.

    **This test is weaker than it looks, and the docstring says so on purpose.**
    A uniform timezone offset shifts the current window and the history by the
    same amount, so the self-join still pairs the same traffic and the computed
    rate is unchanged. That is why the mutation
    `bucket slots in the server's timezone rather than UTC` SURVIVED this test
    and was then withdrawn rather than chased (ADR-0038).

    The defect the UTC cast prevents is a *cross-environment* one — two
    deployments disagreeing about which hour a payment fell in — and no test
    inside a single session can falsify it. Kept as an executable statement of
    intent and a guard against a future change that makes bucketing genuinely
    zone-dependent within one run.
    """
    _stable_pattern(db, quiet_hour_ok=19, quiet_hour_bad=1,
                    busy_hour_ok=2, busy_hour_bad=8,
                    days=7, offset_from=14)
    _stable_pattern(db, quiet_hour_ok=4, quiet_hour_bad=1,
                    busy_hour_ok=4, busy_hour_bad=16,
                    days=7, offset_from=7)

    def measure():
        return seasonal_baseline(db, "MERCH_A", METHOD, window_start=CUT,
                                 history_days=PERIOD_DAYS).as_dict()

    db.execute(text("SET TIME ZONE 'UTC'"))
    in_utc = measure()
    db.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
    shifted = measure()
    db.execute(text("SET TIME ZONE 'UTC'"))

    assert in_utc == shifted, (
        "the baseline changed with the server's timezone: "
        f"{in_utc} vs {shifted}")
