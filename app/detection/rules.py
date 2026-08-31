"""Detection rules — MerchantOps §12.

Detection is deterministic and statistical. The LLM does not inspect raw events:

    millions of events -> deterministic detection -> anomalies -> incidents -> LLM

Everything here is a SQL query and arithmetic. A rule returns `Anomaly` objects;
turning one into an `Incident` is `app.detection.engine`'s job. The split exists
so a rule can be unit-tested against the database without writing anything.

## On the fixed anchor

Rules compare a current window against the preceding one, both measured from
`ANCHOR` rather than `now()`. A real deployment would use wall-clock time; the
fixed anchor is what makes the evaluation suite reproducible, and it is the same
choice `app/tools/investigation.py` already makes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.models import IncidentSeverity, IncidentType

# The dataset's fixed anchor — see the module docstring.
from scripts.seed_data import ANCHOR

DETECTION_VERSION = "detection-v1"

PERIOD_DAYS = 7

# A method must lose at least this many percentage points of success against its
# own baseline before it is an anomaly. Ten points is well outside the seeded
# dataset's ordinary between-period drift (~1 point) and comfortably inside the
# planted degradation (~13 points), so the rule discriminates rather than
# merely firing.
DEGRADATION_THRESHOLD_PP = 10.0

# Below this many attempts in the current window the success rate is noise: a
# method with four attempts can "drop 25 points" from a single failure.
MIN_VOLUME = 30

# Onset detection works on hour buckets, which hold a small fraction of the
# window's volume -- a dozen attempts each, not hundreds. At that size ordinary
# variance clears the method-level threshold easily: two failures in a
# twelve-attempt bucket is an 11-point "drop" and means nothing. So a bucket must
# be both non-trivial and materially worse than the method-level rule requires
# before it counts as part of the degradation.
#
# Without this, `started_at` reports the first noisy hour in the window rather
# than the first degraded one, and §51's incident timeline opens with a time the
# problem had not started.
MIN_BUCKET_VOLUME = 8
ONSET_THRESHOLD_PP = 2 * DEGRADATION_THRESHOLD_PP


@dataclass
class Anomaly:
    """What a rule found. Not yet an incident — the engine decides that."""
    incident_type: IncidentType
    severity: IncidentSeverity
    title: str
    summary: str
    detection_key: str
    detection_rule: str
    revenue_at_risk_minor: int
    signals: dict
    started_at: datetime
    evidence: list[dict] = field(default_factory=list)


def _fmt_inr(minor: int) -> str:
    return f"INR {minor / 100:,.2f}"


def _severity_for(revenue_at_risk_minor: int, drop_pp: float) -> IncidentSeverity:
    """Deterministic severity. MerchantOps §24 keeps risk out of the model, and
    the same applies to how loudly an incident announces itself."""
    if revenue_at_risk_minor >= 500_000_00 or drop_pp >= 40:
        return IncidentSeverity.CRITICAL
    if revenue_at_risk_minor >= 100_000_00 or drop_pp >= 20:
        return IncidentSeverity.HIGH
    if revenue_at_risk_minor >= 20_000_00 or drop_pp >= 12:
        return IncidentSeverity.MEDIUM
    return IncidentSeverity.LOW


def detect_payment_degradation(session, merchant_id: str, *,
                               as_of: datetime | None = None) -> list[Anomaly]:
    """MerchantOps §12: `current_success_rate < baseline - threshold`, per method."""
    as_of = as_of or ANCHOR
    cut = as_of - timedelta(days=PERIOD_DAYS)
    prev_cut = as_of - timedelta(days=PERIOD_DAYS * 2)
    params = {"m": merchant_id, "cut": cut, "prev": prev_cut}

    rows = session.execute(text("""
        SELECT method,
          COUNT(*) FILTER (WHERE created_at >= :cut) AS cur_total,
          COUNT(*) FILTER (WHERE created_at >= :cut AND status <> 'failed') AS cur_ok,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut) AS prev_total,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut
                             AND status <> 'failed') AS prev_ok,
          COALESCE(AVG(amount_minor) FILTER (WHERE created_at >= :cut
                                               AND status <> 'failed'), 0) AS avg_ok_minor
        FROM payments WHERE merchant_id = :m
        GROUP BY method ORDER BY method
    """), params).mappings().all()

    out: list[Anomaly] = []
    for r in rows:
        cur_t, cur_o = int(r["cur_total"]), int(r["cur_ok"])
        pre_t, pre_o = int(r["prev_total"]), int(r["prev_ok"])
        if cur_t < MIN_VOLUME or pre_t < MIN_VOLUME:
            continue

        cur_rate = 100.0 * cur_o / cur_t
        base_rate = 100.0 * pre_o / pre_t
        drop_pp = base_rate - cur_rate
        if drop_pp < DEGRADATION_THRESHOLD_PP:
            continue

        # ---- MerchantOps §22: revenue at risk, computed, never inferred -----
        #   (expected successes - actual successes) x expected transaction value
        # `expected` is what this method's OWN baseline rate would have produced
        # over the volume actually attempted. Using a global rate here would
        # attribute one method's shortfall to another's traffic.
        expected_ok = cur_t * (base_rate / 100.0)
        shortfall = max(0.0, expected_ok - cur_o)
        avg_value = int(r["avg_ok_minor"] or 0)
        revenue_at_risk = int(round(shortfall * avg_value))

        started_at, hourly = _degradation_onset(session, merchant_id, r["method"],
                                                cut, base_rate)

        sev = _severity_for(revenue_at_risk, drop_pp)
        method = r["method"]
        out.append(Anomaly(
            incident_type=IncidentType.PAYMENT_DEGRADATION,
            severity=sev,
            title=f"{method.upper()} payment degradation",
            summary=(
                f"{method.upper()} success rate fell to {cur_rate:.1f}% against a "
                f"baseline of {base_rate:.1f}% ({drop_pp:.1f} points) over the last "
                f"{PERIOD_DAYS} days, across {cur_t} attempts. Estimated revenue at "
                f"risk {_fmt_inr(revenue_at_risk)}."
            ),
            # Idempotency: merchant + type + method + window start. Re-running
            # the sweep over the same window re-derives the same key and the
            # engine's unique constraint refuses the duplicate.
            detection_key=f"{merchant_id}|PAYMENT_DEGRADATION|{method}|{cut.isoformat()}",
            detection_rule="success_rate_below_baseline",
            revenue_at_risk_minor=revenue_at_risk,
            signals={
                "method": method,
                "current_success_rate_pct": round(cur_rate, 1),
                "baseline_success_rate_pct": round(base_rate, 1),
                "drop_pct_points": round(drop_pp, 1),
                "threshold_pct_points": DEGRADATION_THRESHOLD_PP,
                "current_attempts": cur_t,
                "current_failures": cur_t - cur_o,
                "expected_successes": round(expected_ok, 1),
                "actual_successes": cur_o,
                "shortfall_transactions": round(shortfall, 1),
                "average_transaction_value_minor": avg_value,
                "window_start": cut.isoformat(),
                "window_end": as_of.isoformat(),
            },
            started_at=started_at,
            evidence=[
                {"key": "current_success_rate", "value": f"{cur_rate:.1f}%", "source": "payments"},
                {"key": "baseline_success_rate", "value": f"{base_rate:.1f}%", "source": "payments"},
                {"key": "drop_pct_points", "value": round(drop_pp, 1), "source": "payments"},
                {"key": "failed_transactions", "value": cur_t - cur_o, "source": "payments"},
                {"key": "revenue_at_risk", "value": _fmt_inr(revenue_at_risk),
                 "source": "calculation_engine"},
                {"key": "worst_hours", "value": hourly, "source": "payments"},
            ],
        ))
    return out


def _degradation_onset(session, merchant_id: str, method: str,
                       cut: datetime, base_rate: float) -> tuple[datetime, list[str]]:
    """When the degradation first showed, and which hours are worst.

    `started_at` is the earliest timestamp in the window inside an hour bucket
    whose success rate is below baseline by the threshold. MerchantOps §51 shows
    an incident timeline opening with "18:07 detected"; without this the
    incident could only report the window start, which is a different claim.
    """
    rows = session.execute(text("""
        SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::int AS hr,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status <> 'failed') AS ok,
               MIN(created_at) FILTER (WHERE status = 'failed') AS first_fail
        FROM payments
        WHERE merchant_id = :m AND method = :meth AND created_at >= :cut
        GROUP BY hr ORDER BY hr
    """), {"m": merchant_id, "meth": method, "cut": cut}).mappings().all()

    bad = []
    for r in rows:
        total = int(r["total"])
        if total < MIN_BUCKET_VOLUME:
            continue
        rate = 100.0 * int(r["ok"]) / total
        if base_rate - rate >= ONSET_THRESHOLD_PP:
            bad.append((int(r["hr"]), rate, r["first_fail"], total))

    if not bad:
        return cut, []

    bad.sort(key=lambda x: x[1])
    worst = [f"{h:02d}:00 ({rate:.0f}% success, {n} attempts)" for h, rate, _, n in bad[:3]]
    onsets = [b[2] for b in bad if b[2] is not None]
    if not onsets:
        return cut, worst
    # Normalise to UTC. The column is timestamptz and the driver renders it in
    # the session's timezone, so the naive `.hour` of this value is whatever the
    # server happens to be set to -- which is not the hour the bucket was keyed
    # on (that one is explicitly `AT TIME ZONE 'UTC'`). Reporting an onset in an
    # unstated zone is how a timeline becomes wrong by a whole-hours offset.
    return min(onsets).astimezone(timezone.utc), worst


def detect_duplicate_payments(session, merchant_id: str, *,
                              window_seconds: int = 600,
                              as_of: datetime | None = None) -> list[Anomaly]:
    """Duplicate charges against one order. A revenue-integrity problem the
    merchant owes the customer money for, so it is an incident, not a metric.

    One incident per ORDER, not per pair. The first implementation emitted a
    pair at a time, which is right for two captures and wrong for three: an
    order charged three times produced three overlapping incidents, each
    claiming the full amount at risk, for a total of 3x an exposure that is
    actually 2x. Nothing on the shipped dataset triggers it -- the seeded triple
    sits outside the detection window -- so this is a latent overcount rather
    than an observed one, and a revenue figure that is wrong only on data we
    happen not to have is still wrong.

    The earliest capture is the legitimate payment. Everything after it is
    excess, and the exposure is what remains unrefunded across the excess.
    """
    as_of = as_of or ANCHOR
    cut = as_of - timedelta(days=PERIOD_DAYS)

    rows = session.execute(text("""
        SELECT p.order_id, p.customer_id, p.amount_minor, p.method,
               p.id, p.created_at, p.amount_refunded_minor
        FROM payments p
        JOIN (
            SELECT order_id, customer_id, amount_minor
            FROM payments
            WHERE merchant_id = :m AND status IN ('captured','refunded')
            GROUP BY order_id, customer_id, amount_minor
            HAVING COUNT(*) > 1
        ) g ON g.order_id = p.order_id AND g.customer_id = p.customer_id
           AND g.amount_minor = p.amount_minor
        WHERE p.merchant_id = :m AND p.status IN ('captured','refunded')
        ORDER BY p.order_id, p.created_at, p.id
    """), {"m": merchant_id}).mappings().all()

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["order_id"], r["customer_id"], r["amount_minor"]), []).append(dict(r))

    out: list[Anomaly] = []
    for (order_id, customer_id, amount_minor), members in sorted(groups.items()):
        first, *excess = members
        # Only excess captures close behind the first one are duplicates; a
        # legitimate second purchase of the same item days later is not.
        excess = [e for e in excess
                  if (e["created_at"] - first["created_at"]).total_seconds() <= window_seconds]
        if not excess:
            continue
        # In-window means the duplicate HAPPENED recently, judged on the latest
        # excess capture. An old duplicate is history, not an open incident.
        latest = max(e["created_at"] for e in excess)
        if latest < cut:
            continue

        exposure = sum(int(e["amount_minor"]) - int(e["amount_refunded_minor"])
                       for e in excess)
        if exposure <= 0:
            # Already refunded in full: the money is back, and raising an
            # incident would be raising a resolved problem.
            continue

        gaps = [int((e["created_at"] - first["created_at"]).total_seconds()) for e in excess]
        ids = [e["id"] for e in excess]
        out.append(Anomaly(
            incident_type=IncidentType.DUPLICATE_PAYMENT,
            severity=_severity_for(exposure, 0.0),
            title=f"Duplicate payment on {order_id}",
            summary=(
                f"Order {order_id} carries {len(members)} captured payments of "
                f"{_fmt_inr(int(amount_minor))} from customer {customer_id}. "
                f"{first['id']} is the original; {', '.join(ids)} "
                f"{'is' if len(ids) == 1 else 'are'} excess. Unrefunded exposure "
                f"{_fmt_inr(exposure)}."
            ),
            detection_key=f"{merchant_id}|DUPLICATE_PAYMENT|{order_id}|{amount_minor}",
            detection_rule="duplicate_capture_on_order",
            revenue_at_risk_minor=exposure,
            signals={
                "order_id": order_id, "customer_id": customer_id,
                "first_payment_id": first["id"],
                "excess_payment_ids": ids,
                "capture_count": len(members),
                "amount_minor": int(amount_minor),
                "time_separation_seconds": gaps,
                "already_refunded_minor": sum(int(e["amount_refunded_minor"]) for e in excess),
                "window_seconds": window_seconds,
            },
            started_at=latest,
            evidence=[
                {"key": "original_payment", "value": first["id"], "source": "payments"},
                {"key": "excess_payments", "value": ids, "source": "payments"},
                {"key": "capture_count", "value": len(members), "source": "payments"},
                {"key": "amount", "value": _fmt_inr(int(amount_minor)), "source": "payments"},
                {"key": "time_separation_seconds", "value": gaps, "source": "payments"},
                {"key": "unrefunded_exposure", "value": _fmt_inr(exposure),
                 "source": "calculation_engine"},
            ],
        ))
    return out


RULES = (detect_payment_degradation, detect_duplicate_payments)
