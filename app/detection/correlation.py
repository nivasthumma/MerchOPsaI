"""Multivariate correlation — MerchantOps v2 §18.

§18's argument, in its own example:

    UPI success ↓
    Latency ↑
    Revenue ↓
    Checkout conversion ↓

    Individually:  Possible anomaly
    Together:      High-confidence operational incident

Every detection rule in this build fires on one signal. That is enough to
notice something and not enough to know how much to believe it, and the
`Anomaly` docstring has always said as much — "Not yet an incident, the engine
decides that" — while the engine in fact persisted each one unexamined.

## What this does, and what it deliberately does not

It **annotates**. Anomalies that overlap in time and concern the same merchant
are grouped, each incident records how many other independent signals coincided
with it, and that count flows into `app.agent.confidence` as corroboration.

It does **not** suppress. A lone anomaly still becomes an incident. Two reasons,
and the second is the real one:

1. §18 places correlation "before triggering deeper AI investigation" — before
   the expensive step, not before the record. An incident nobody opened is
   indistinguishable from a system that missed it.
2. Deciding that a signal is too weak to be worth recording is a decision about
   what a merchant is allowed to find out. That belongs to a threshold someone
   configures deliberately, not to a default introduced alongside the mechanism
   that makes it possible.

So this makes corroboration *visible and usable*. Acting on it by declining to
open an incident is a separate change, and a policy one.

## Independence

Two anomalies corroborate only if they come from **different rules**. Two
findings from `success_rate_below_baseline` — UPI and cards, say — are the same
instrument reporting twice; treating that as two independent signals is how a
single sensor fault reads as a confirmed outage. This is the same rule
`app.agent.confidence` applies to evidence sources, for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

# Anomalies whose windows start within this of each other are treated as
# concerning the same episode. Wide enough that a rule reading an hourly bucket
# and one reading a rolling window still line up; narrow enough that this
# morning's degradation does not corroborate this afternoon's.
CORRELATION_WINDOW = timedelta(minutes=30)


@dataclass
class Cluster:
    """Anomalies that appear to describe one episode."""
    anomalies: list = field(default_factory=list)

    @property
    def independent_rules(self) -> list[str]:
        """Distinct rules, sorted. The count of these is the corroboration."""
        return sorted({a.detection_rule for a in self.anomalies})

    @property
    def corroboration(self) -> int:
        return len(self.independent_rules)

    @property
    def revenue_at_risk_minor(self) -> int:
        """The episode's total exposure.

        Summed across anomalies, which overstates it whenever two rules see the
        same lost payment from different angles. Reported as
        `cluster_revenue_at_risk_minor` and never written to
        `Incident.revenue_at_risk_minor`, because that field is the calculation
        engine's (MerchantOps §22, §34) and a sum of overlapping estimates is
        not a calculation.
        """
        return sum(a.revenue_at_risk_minor for a in self.anomalies)


def cluster(anomalies: list) -> list[Cluster]:
    """Group anomalies into episodes by overlapping start time.

    Single-link over a sorted list: an anomaly joins the running cluster while
    it starts within `CORRELATION_WINDOW` of the previous one. Chaining is
    intended — a degradation at 18:07, a revenue dip at 18:30 and a conversion
    drop at 18:55 are one episode, even though the first and last are 48
    minutes apart.
    """
    if not anomalies:
        return []

    ordered = sorted(anomalies, key=lambda a: a.started_at)
    clusters: list[Cluster] = [Cluster([ordered[0]])]
    for a in ordered[1:]:
        previous = clusters[-1].anomalies[-1]
        if a.started_at - previous.started_at <= CORRELATION_WINDOW:
            clusters[-1].anomalies.append(a)
        else:
            clusters.append(Cluster([a]))
    return clusters


def annotate(anomalies: list) -> dict[str, dict]:
    """Correlation facts per anomaly, keyed by `detection_key`.

    Returned as a mapping rather than mutated onto the anomalies so that the
    engine decides what to persist. A rule produces observations; what they add
    up to is the engine's question, which is what the `Anomaly` docstring
    claimed all along.
    """
    facts: dict[str, dict] = {}
    for c in cluster(anomalies):
        for a in c.anomalies:
            others = [r for r in c.independent_rules if r != a.detection_rule]
            facts[a.detection_key] = {
                # How many independent rules saw this episode, including this
                # one. 1 means uncorroborated.
                "corroboration": c.corroboration,
                "corroborating_rules": others,
                "cluster_size": len(c.anomalies),
                "cluster_revenue_at_risk_minor": c.revenue_at_risk_minor,
                # v2 §18's distinction, named so a reader does not have to
                # infer it from a number.
                "multivariate": c.corroboration > 1,
            }
    return facts
