"""The confidence band is computed, not claimed — MerchantOps v2 §33.

The assertion this file exists for is the last one: a model that reports 0.99
cannot raise the band, and a model that reports 0.1 can lower it. Everything
else is the arithmetic that makes that assertion meaningful.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agent.confidence import FRESHNESS_WINDOW, Confidence, assess


class Ev:
    """The structural shape `assess` reads — an IncidentEvidence in practice."""
    def __init__(self, source: str, *, untrusted: bool = False, age=None):
        self.source = source
        self.untrusted = untrusted
        self.created_at = datetime.now(UTC) - (age or timedelta(0))


class Tool:
    def __init__(self, success: bool = True):
        self.success = success


# ------------------------------------------------------------------ the bands
def test_no_evidence_is_insufficient_rather_than_low():
    """"We have not got enough to look at" is not "we looked and are unsure".

    They lead to different next actions, which is why MerchantOps keeps FAILED
    and UNKNOWN apart too (§53).
    """
    a = assess(evidence=[])
    assert a.band is Confidence.INSUFFICIENT
    assert "no trusted evidence" in a.reasons[0]


def test_several_fresh_independent_sources_reach_high():
    a = assess(evidence=[Ev("payments"), Ev("calculation_engine"), Ev("orders")])
    assert a.band is Confidence.HIGH
    assert a.independent_sources == 3


def test_one_source_is_low_however_many_rows_it_produced():
    """v2 §18's argument: one signal is an anomaly, not an incident.

    Five readings from the same table are one signal read five times.
    """
    a = assess(evidence=[Ev("payments") for _ in range(5)])
    assert a.band is Confidence.LOW
    assert a.independent_sources == 1
    assert a.trusted_evidence == 5


def test_two_sources_are_medium():
    a = assess(evidence=[Ev("payments"), Ev("calculation_engine")])
    assert a.band is Confidence.MEDIUM


# ------------------------------------------------------------------- untrusted
def test_untrusted_evidence_never_corroborates():
    """MerchantOps §39: order and customer text is data, not instruction.

    It is equally not corroboration. If it were, someone who can write an order
    note could raise the system's stated confidence in a conclusion they chose.
    """
    a = assess(evidence=[Ev("payments"),
                         Ev("order_notes", untrusted=True),
                         Ev("customer_notes", untrusted=True)])
    assert a.trusted_evidence == 1
    assert a.independent_sources == 1
    assert a.band is Confidence.LOW      # not MEDIUM, not HIGH


def test_only_untrusted_evidence_is_insufficient():
    a = assess(evidence=[Ev("order_notes", untrusted=True)])
    assert a.band is Confidence.INSUFFICIENT
    assert "untrusted" in a.reasons[0]


# ----------------------------------------------------------------- freshness
def test_stale_evidence_cannot_reach_high():
    old = FRESHNESS_WINDOW + timedelta(minutes=5)
    a = assess(evidence=[Ev("payments"), Ev("calculation_engine"),
                         Ev("orders", age=old)])
    assert a.stale_evidence == 1
    assert a.band is Confidence.MEDIUM


def test_evidence_that_is_entirely_stale_is_low():
    old = FRESHNESS_WINDOW + timedelta(hours=1)
    a = assess(evidence=[Ev("payments", age=old), Ev("orders", age=old)])
    assert a.agreeing_signals == 0
    assert a.band is Confidence.LOW


# --------------------------------------------------------- provider / tooling
def test_provider_confirmation_lifts_medium_but_cannot_rescue_low():
    """The one input that did not originate inside this system.

    It can widen a case that is already real. It cannot make a thin case broad:
    confirming one fact externally is not the same as having several.
    """
    lifted = assess(evidence=[Ev("payments"), Ev("razorpay")])
    assert lifted.provider_confirmed is True
    assert lifted.band is Confidence.HIGH

    thin = assess(evidence=[Ev("razorpay")])
    assert thin.provider_confirmed is True
    assert thin.band is Confidence.LOW        # one source is still one source


def test_provider_confirmation_is_derived_from_the_evidence_not_asserted():
    assert assess(evidence=[Ev("payments")]).provider_confirmed is False
    assert assess(evidence=[Ev("webhook_events")]).provider_confirmed is True


def test_a_failed_tool_call_is_a_question_that_went_unanswered():
    full = [Ev("payments"), Ev("calculation_engine"), Ev("orders")]
    assert assess(evidence=full).band is Confidence.HIGH
    assert assess(evidence=full, tool_calls=[Tool(success=False)]).band is Confidence.MEDIUM


# ------------------------------------------------------- the model's own number
def test_a_confident_model_cannot_raise_the_band():
    """The assertion this module exists for.

    A model asserting its own reliability is the one claim it has no standing
    to make. 0.99 buys nothing that the evidence did not already support.
    """
    thin = [Ev("payments")]
    assert assess(evidence=thin, model_confidence=0.99).band is Confidence.LOW
    assert assess(evidence=thin, model_confidence=1.0).band is Confidence.LOW
    # And it does not manufacture evidence out of an empty case either.
    assert assess(evidence=[], model_confidence=1.0).band is Confidence.INSUFFICIENT


def test_a_hedging_model_can_lower_the_band():
    """A model that volunteers doubt is telling us something we cannot see."""
    strong = [Ev("payments"), Ev("calculation_engine"), Ev("orders")]
    assert assess(evidence=strong).band is Confidence.HIGH
    assert assess(evidence=strong, model_confidence=0.5).band is Confidence.MEDIUM
    assert assess(evidence=strong, model_confidence=0.1).band is Confidence.LOW


def test_the_cap_is_recorded_so_the_band_can_be_explained():
    a = assess(evidence=[Ev("payments"), Ev("calculation_engine"), Ev("orders")],
               model_confidence=0.2)
    assert a.band is Confidence.LOW
    assert any("capped at LOW" in r for r in a.reasons)
    assert a.model_confidence == 0.2


def test_the_assessment_carries_every_input_it_used():
    """A band with no derivation is the opaque number this replaces."""
    a = assess(evidence=[Ev("payments"), Ev("razorpay"),
                         Ev("notes", untrusted=True)],
               tool_calls=[Tool(success=True), Tool(success=False)],
               model_confidence=0.8)
    d = a.as_dict()
    for key in ("band", "total_evidence", "trusted_evidence",
                "independent_sources", "agreeing_signals", "stale_evidence",
                "provider_confirmed", "failed_tool_calls", "model_confidence",
                "reasons"):
        assert key in d, key
    assert d["total_evidence"] == 3
    assert d["trusted_evidence"] == 2
    assert d["failed_tool_calls"] == 1
