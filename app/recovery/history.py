"""What past recovery actually achieved — MerchantOps v2 §40.

§40's figure:

    Payment retry:  21% recovery
    Payment link:   48% recovery
    Notification:   13% recovery

    For a new cohort, the recovery planner can consider these outcomes.
    The AI can reason over them.
    However, strategy execution remains subject to deterministic policy.

That last line is the whole design constraint, and it is easy to lose. The
obvious reading of §40 — "pick whichever intervention has the best historical
rate" — would break a safety rule this build already holds. `_INTERVENTION` in
the planner maps a degraded method to PAYMENT_LINK rather than RETRY, and its
comment says why: "re-presenting a customer to the same rail that is currently
failing is not a recovery, it is the same failure again." If history showed
RETRY recovering at 60%, letting that select RETRY for a degradation would send
customers back to the broken rail because it used to work.

So history does two things here and is forbidden a third:

    it MAY sharpen the estimate of what a chosen intervention is worth
    it MAY rank interventions the deterministic mapping already permits
    it MAY NOT widen that set

## No history is not a zero rate

An intervention nobody has attempted has no measured rate. Reporting that as 0%
would make it look thoroughly tested and useless, and would suppress it
permanently — the intervention never gets tried, so it never accumulates
history, so it stays at 0%. `Outcome.measured` is False in that case and callers
fall back to the estimate they used before, saying so.

This is the same distinction as UNKNOWN against FAILED (§53), INSUFFICIENT
against LOW (§33) and UNTESTED against REJECTED (§30). It keeps coming up
because it keeps being the difference between "we looked" and "we cannot see".
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from app.models import Intervention

# Below this many settled attempts, a rate is an anecdote. Three successes out
# of three is not a 100% recovery rate; it is three successes. The threshold is
# deliberately low because a merchant will not have thousands of campaigns
# before the first estimate matters, and deliberately not one.
MIN_SAMPLE = 10

# Statuses that count as *settled* — the attempt finished and we know what
# happened. ATTEMPTED is excluded: it is still in flight, and counting it as a
# non-recovery would depress every rate by however much work is currently
# running. UNKNOWN is included and counted as a non-recovery, because §53's
# whole point is that an unresolved external state is not a success.
_SETTLED = ("RECOVERED", "FAILED", "UNKNOWN")


@dataclass(frozen=True)
class Outcome:
    """What one intervention has actually achieved for one merchant."""
    intervention: Intervention
    attempts: int
    recovered: int
    attempted_minor: int
    recovered_minor: int
    measured: bool          # False when there is not enough history to have a view

    @property
    def rate(self) -> float | None:
        """Recovery rate by count, or None when unmeasured.

        None rather than 0.0. A caller that treats the two as the same is the
        bug this type exists to prevent, and `None` makes that a TypeError
        instead of a quietly pessimistic number.
        """
        if not self.measured or not self.attempts:
            return None
        return self.recovered / self.attempts

    @property
    def value_rate(self) -> float | None:
        """Recovery rate by VALUE, which is the one that matters for money.

        Recovering nine ₹100 payments and losing one ₹10,000 payment is a 90%
        count rate and a 10% value rate. The planner estimates money, so it
        uses this one; §40's figure is a count rate and both are reported.
        """
        if not self.measured or not self.attempted_minor:
            return None
        return self.recovered_minor / self.attempted_minor

    def as_dict(self) -> dict:
        return {
            "intervention": self.intervention.value,
            "attempts": self.attempts,
            "recovered": self.recovered,
            "attempted_minor": self.attempted_minor,
            "recovered_minor": self.recovered_minor,
            "measured": self.measured,
            "rate": self.rate,
            "value_rate": self.value_rate,
            "min_sample": MIN_SAMPLE,
        }


def outcomes(session, merchant_id: str) -> dict[Intervention, Outcome]:
    """Measured recovery per intervention, for one merchant.

    Scoped to the merchant, never global. Another merchant's payment links
    converting well says nothing about this one's customers, and pooling them
    would leak one merchant's commercial performance into another's plan --
    a cross-tenant read wearing a statistic (§54).
    """
    # `= ANY(:settled)` rather than an IN-list spliced into the string: the
    # statuses are ours and safe either way, but a bound array is one fewer
    # place where a query is assembled by concatenation.
    rows = session.execute(text("""
        SELECT intervention,
               COUNT(*)                                                  AS attempts,
               COUNT(*) FILTER (WHERE status = 'RECOVERED')              AS recovered,
               COALESCE(SUM(attributed_amount_minor), 0)                 AS attempted_minor,
               COALESCE(SUM(actual_recovery_minor), 0)                   AS recovered_minor
        FROM recovery_candidates
        WHERE merchant_id = :m
          AND status = ANY(:settled)
        GROUP BY intervention
    """), {"m": merchant_id, "settled": list(_SETTLED)}).mappings().all()

    out: dict[Intervention, Outcome] = {}
    for r in rows:
        try:
            intervention = Intervention(r["intervention"])
        except ValueError:            # a retired intervention still in the table
            continue
        attempts = int(r["attempts"])
        out[intervention] = Outcome(
            intervention=intervention,
            attempts=attempts,
            recovered=int(r["recovered"]),
            attempted_minor=int(r["attempted_minor"]),
            recovered_minor=int(r["recovered_minor"]),
            measured=attempts >= MIN_SAMPLE,
        )
    return out


def outcome_for(session, merchant_id: str,
                intervention: Intervention) -> Outcome:
    """One intervention's record. Never None — an absent row is an unmeasured
    outcome, which is a fact about it rather than a missing answer."""
    found = outcomes(session, merchant_id).get(intervention)
    if found is not None:
        return found
    return Outcome(intervention=intervention, attempts=0, recovered=0,
                   attempted_minor=0, recovered_minor=0, measured=False)


def rank(session, merchant_id: str,
         permitted: list[Intervention]) -> list[Outcome]:
    """Order the interventions the planner already permits, best measured first.

    `permitted` is the deterministic mapping's answer and this function cannot
    add to it. That is §40's "strategy execution remains subject to
    deterministic policy" expressed as a signature: there is no argument here
    that could introduce an intervention the planner did not already consider
    safe for this incident.

    Unmeasured interventions sort last, and among themselves keep the order
    they were given. They are not ranked below measured ones because they are
    worse — nothing is known about them — but an unmeasured option cannot
    displace one with evidence behind it.
    """
    known = outcomes(session, merchant_id)
    scored = [known.get(i) or Outcome(i, 0, 0, 0, 0, False) for i in permitted]
    return sorted(
        scored,
        key=lambda o: (0 if o.measured else 1, -(o.value_rate or 0.0)),
    )
