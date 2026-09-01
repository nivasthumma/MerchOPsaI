"""Adaptive baselines — MerchantOps v2 §17.

§17's complaint about the rule this build already has:

    Static thresholds are insufficient.

    The platform should eventually compare:
        Current Monday 18:00
    against:
        Previous Mondays 18:00

    ...and account for day of week, hour, seasonality, merchant traffic,
    payment method, customer segment.

    This prevents normal traffic patterns from becoming false incidents.

That last line is the specification of what this is *for*, and it decides the
design. §17 is not asking for a second detector. It is asking for a way to tell
a genuine drop from a Tuesday evening.

## So the seasonal baseline is a veto, never a trigger

`detect_payment_degradation` fires as it always did. Before the anomaly becomes
an incident, this module asks a narrower question: **is the drop explained by
when it happened?** If the current window's success rate is consistent with what
those specific hours have historically done, the anomaly is suppressed.

It can only ever remove an incident, never add one. That asymmetry is
deliberate:

- A false incident spends an agent budget, opens a console entry a human must
  dismiss, and teaches a merchant that the console cries wolf. §17 exists to
  stop that.
- A *missed* incident is money nobody notices. Letting a seasonal model create
  incidents would mean a bug in this file could invent them; letting it only
  suppress means a bug here can at worst leave today's behaviour in place.

## Sample sufficiency decides which baseline is used

A same-weekday, same-hour comparison needs several prior same-weekday
same-hours. With a fortnight of history there is one, which is not a baseline;
it is a single earlier reading. So the comparison degrades honestly:

    DOW_HOUR   this weekday at this hour, historically   (needs MIN_SLOT_SAMPLES)
    HOUR       this hour on any day                      (needs MIN_SLOT_SAMPLES)
    FLAT       no seasonal opinion; the caller keeps its own baseline

`Baseline.granularity` records which was used, and it is written onto the
incident's signals. A suppression nobody can attribute to a level of evidence is
a suppression nobody can argue with, which for a control that *hides incidents*
is the wrong way round.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

# How many historical observations a slot needs before its rate is a baseline
# rather than an anecdote. Two same-weekday readings can disagree by twenty
# points on ordinary variance; the guard is what stops a thin slot vetoing a
# real incident.
MIN_SLOT_SAMPLES = 3

# Attempts in a slot, historically, before that slot counts as observed at all.
MIN_SLOT_VOLUME = 5

# How much of the current window's volume must fall in slots we actually have
# history for. Below this the seasonal picture is mostly guesswork stitched over
# gaps, and guesswork must not suppress an incident.
MIN_COVERAGE = 0.6


@dataclass(frozen=True)
class Baseline:
    """A seasonally-adjusted expectation for one method's current window."""
    granularity: str            # DOW_HOUR | HOUR | FLAT
    expected_rate: float | None  # percent, or None when FLAT
    coverage: float             # share of current volume with history behind it
    slots: int                  # distinct slots that contributed
    samples: int                # historical observations behind them

    @property
    def measured(self) -> bool:
        return self.expected_rate is not None

    def as_dict(self) -> dict:
        return {
            "granularity": self.granularity,
            "expected_rate_pct": (round(self.expected_rate, 1)
                                  if self.expected_rate is not None else None),
            "coverage": round(self.coverage, 2),
            "slots": self.slots,
            "samples": self.samples,
            "min_slot_samples": MIN_SLOT_SAMPLES,
        }


_FLAT = Baseline("FLAT", None, 0.0, 0, 0)

# Seasonal keys, strongest first. Both bucket by hour; DOW_HOUR additionally
# separates weekdays, which is §17's "Current Monday 18:00 against previous
# Mondays 18:00". HOUR is the fallback when a fortnight of history gives each
# weekday-hour a single reading.
_LEVELS = ("DOW_HOUR", "HOUR")


def seasonal_baseline(session, merchant_id: str, method: str, *,
                      window_start: datetime, history_days: int) -> Baseline:
    """What this method's success rate has historically been, at these hours.

    Weighted by the CURRENT window's volume per slot, not by the historical
    volume. The question is "what should this particular window have looked
    like", and a slot the merchant barely trades in must not dominate the
    expectation just because it has a long history.
    """
    history_start = window_start - timedelta(days=history_days)

    for granularity in _LEVELS:
        # AT TIME ZONE 'UTC' is load-bearing. `created_at` is timestamptz and
        # EXTRACT renders it in the SESSION's zone, so the same rows bucket into
        # different hours depending on the server's TimeZone setting -- seeded
        # 17:00 UTC lands in hour 3 on a +10 connection. A seasonal baseline
        # whose slots move with the server is not a baseline.
        #
        # `app/eval/runner.py` already carries this lesson for `onset_hour`;
        # this file learned it a second time, from a fixture that appeared to
        # break the veto and was really bucketing against different hours.
        utc = "(created_at AT TIME ZONE 'UTC')"
        dow_expr = (f"EXTRACT(ISODOW FROM {utc})::int"
                    if granularity == "DOW_HOUR" else "0")
        row = session.execute(text(f"""
            WITH cur AS (
                SELECT {dow_expr} AS dow,
                       EXTRACT(HOUR FROM {utc})::int AS hr,
                       COUNT(*) AS n
                FROM payments
                WHERE merchant_id = :m AND method = :method
                  AND created_at >= :window_start
                GROUP BY 1, 2
            ),
            hist AS (
                SELECT {dow_expr} AS dow,
                       EXTRACT(HOUR FROM {utc})::int AS hr,
                       COUNT(*) AS attempts,
                       COUNT(*) FILTER (WHERE status <> 'failed') AS ok,
                       COUNT(DISTINCT ({utc})::date) AS observations
                FROM payments
                WHERE merchant_id = :m AND method = :method
                  AND created_at >= :history_start AND created_at < :window_start
                GROUP BY 1, 2
            ),
            usable AS (
                SELECT cur.n, hist.ok, hist.attempts, hist.observations
                FROM cur JOIN hist USING (dow, hr)
                WHERE hist.observations >= :min_samples
                  AND hist.attempts     >= :min_volume
            )
            SELECT
              (SELECT COALESCE(SUM(n), 0) FROM cur)                      AS cur_total,
              (SELECT COALESCE(SUM(n), 0) FROM usable)                   AS covered,
              (SELECT COUNT(*) FROM usable)                              AS slots,
              (SELECT COALESCE(SUM(observations), 0) FROM usable)        AS samples,
              (SELECT COALESCE(SUM(n * ok::float / NULLIF(attempts, 0)), 0)
                 FROM usable)                                            AS weighted_ok
        """), {"m": merchant_id, "method": method,
               "window_start": window_start, "history_start": history_start,
               "min_samples": MIN_SLOT_SAMPLES,
               "min_volume": MIN_SLOT_VOLUME}).mappings().one()

        cur_total = int(row["cur_total"] or 0)
        covered = int(row["covered"] or 0)
        if not cur_total or not covered:
            continue

        coverage = covered / cur_total
        if coverage < MIN_COVERAGE:
            # Enough history to be tempting, not enough to be trusted. Try the
            # coarser level rather than extrapolating over the gaps.
            continue

        # The rate those slots would have produced over THIS window's volume.
        expected = 100.0 * float(row["weighted_ok"]) / covered
        return Baseline(granularity, expected, coverage,
                        int(row["slots"]), int(row["samples"]))

    return _FLAT


def explains_drop(baseline: Baseline, current_rate: float,
                  threshold_pp: float) -> bool:
    """Is the observed rate consistent with what these hours normally do?

    True only when there is a measured seasonal expectation AND the current rate
    is within the same threshold of it that the flat rule uses. Reusing the
    threshold is deliberate: a drop the flat rule would not have called an
    anomaly against the seasonal baseline is, by the flat rule's own standard,
    not an anomaly.

    Never true for a FLAT baseline. No opinion is not agreement.
    """
    if not baseline.measured:
        return False
    # float() because a rate computed in SQL arrives as Decimal, and mixing the
    # two raises rather than comparing. The caller in `rules.py` happens to pass
    # a float; a caller that did not would fail at the moment the veto was
    # being decided, which is the worst place to discover a type.
    return (baseline.expected_rate - float(current_rate)) < threshold_pp
