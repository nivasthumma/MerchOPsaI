"""Multivariate correlation — MerchantOps v2 §18.

§18's claim is that one signal is an anomaly and several agreeing ones are an
incident. The assertions that carry it are the two about independence: two
findings from the *same* rule do not corroborate each other, and two findings
hours apart do not describe the same episode.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.detection.correlation import CORRELATION_WINDOW, annotate, cluster


class A:
    """The structural shape correlation reads — an Anomaly in practice."""
    def __init__(self, rule: str, key: str, *, at=None, risk: int = 0):
        self.detection_rule = rule
        self.detection_key = key
        self.started_at = at or datetime(2026, 9, 1, 18, 7, tzinfo=UTC)
        self.revenue_at_risk_minor = risk


BASE = datetime(2026, 9, 1, 18, 7, tzinfo=UTC)


# -------------------------------------------------------------------- clusters
def test_nothing_produces_nothing():
    assert cluster([]) == []
    assert annotate([]) == {}


def test_signals_in_the_same_window_form_one_episode():
    """v2 §18's own example: degradation, revenue, conversion, together."""
    signals = [
        A("success_rate_below_baseline", "k1", at=BASE),
        A("revenue_below_expected", "k2", at=BASE + timedelta(minutes=10)),
        A("conversion_drop", "k3", at=BASE + timedelta(minutes=20)),
    ]
    clusters = cluster(signals)
    assert len(clusters) == 1
    assert clusters[0].corroboration == 3


def test_signals_far_apart_are_separate_episodes():
    """This morning's degradation does not corroborate this afternoon's."""
    signals = [
        A("success_rate_below_baseline", "k1", at=BASE),
        A("revenue_below_expected", "k2", at=BASE + timedelta(hours=4)),
    ]
    assert len(cluster(signals)) == 2


def test_an_episode_may_chain_beyond_the_window():
    """A degradation at 18:07, a dip at 18:30, a drop at 18:55 is one episode.

    Single-link is deliberate: the alternative is a fixed bucket, which splits
    an episode in two whenever it happens to straddle a boundary.
    """
    signals = [
        A("r1", "k1", at=BASE),
        A("r2", "k2", at=BASE + timedelta(minutes=25)),
        A("r3", "k3", at=BASE + timedelta(minutes=50)),
    ]
    clusters = cluster(signals)
    assert len(clusters) == 1
    assert clusters[0].anomalies[-1].started_at - clusters[0].anomalies[0].started_at \
        > CORRELATION_WINDOW


# ---------------------------------------------------------------- independence
def test_the_same_rule_twice_is_one_signal_not_two():
    """The assertion §18 turns on.

    UPI and cards both failing the success-rate rule is one instrument
    reporting twice. Counting that as two independent signals is how a single
    sensor fault reads as a confirmed outage.
    """
    signals = [
        A("success_rate_below_baseline", "upi", at=BASE),
        A("success_rate_below_baseline", "card", at=BASE + timedelta(minutes=5)),
    ]
    c = cluster(signals)[0]
    assert len(c.anomalies) == 2
    assert c.corroboration == 1                 # one rule, however many rows
    assert c.independent_rules == ["success_rate_below_baseline"]

    facts = annotate(signals)
    assert facts["upi"]["multivariate"] is False
    assert facts["upi"]["corroborating_rules"] == []


def test_different_rules_corroborate_each_other_and_name_which():
    signals = [
        A("success_rate_below_baseline", "k1", at=BASE),
        A("revenue_below_expected", "k2", at=BASE + timedelta(minutes=5)),
    ]
    facts = annotate(signals)
    assert facts["k1"]["corroboration"] == 2
    assert facts["k1"]["multivariate"] is True
    # Each is told about the OTHER, not about itself.
    assert facts["k1"]["corroborating_rules"] == ["revenue_below_expected"]
    assert facts["k2"]["corroborating_rules"] == ["success_rate_below_baseline"]


def test_a_lone_signal_is_annotated_as_uncorroborated_not_dropped():
    """Correlation annotates; it does not suppress.

    An incident nobody opened is indistinguishable from a system that missed
    it, and deciding a signal is too weak to record is a policy decision rather
    than a side effect of building the mechanism.
    """
    facts = annotate([A("success_rate_below_baseline", "alone", at=BASE)])
    assert facts["alone"]["corroboration"] == 1
    assert facts["alone"]["multivariate"] is False


# --------------------------------------------------------------------- exposure
def test_cluster_exposure_is_reported_separately_from_the_incidents_own():
    """A sum of overlapping estimates is not a calculation (MerchantOps §22)."""
    signals = [A("r1", "k1", at=BASE, risk=100_00),
               A("r2", "k2", at=BASE + timedelta(minutes=1), risk=250_00)]
    facts = annotate(signals)
    assert facts["k1"]["cluster_revenue_at_risk_minor"] == 350_00
    # And it is a distinct key from the incident's own figure, which the
    # calculation engine owns.
    assert "revenue_at_risk_minor" not in facts["k1"]
