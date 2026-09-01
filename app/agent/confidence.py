"""Confidence is computed, not claimed — MerchantOps v2 §33.

v2 §33 is one of the document's sharper instructions:

    LLM confidence should not be blindly trusted.

and it names the six things a platform-owned confidence model should weigh
instead: evidence quality, evidence agreement, data freshness, historical
consistency, provider confirmation, and the number of independent signals.

## Why this is a correctness gap and not a feature

Until now `task.agent_confidence` was the float the model emitted, stored
verbatim. Both the model docstring and the API said, correctly, that it "gates
nothing" — and that was true and sufficient while it was an internal number.
It stops being sufficient the moment it is rendered to a merchant as the
system's confidence, which is exactly what v2 §63 and §102 show:

    Confidence:
    HIGH

That screen is a claim by the platform. A platform must not make a claim whose
only support is that the thing being assessed said so. This is v2 §89 Rule 5
("financial calculations are deterministic") applied one level up: if a number
informs a decision a person will make about money, the platform owns it.

## The asymmetry

The model's own number is still an input — it is evidence about the model's
state, which is worth something — but a **bounded** one:

    the model may LOWER the band. It may never raise it.

This mirrors `requires_human` in `app/agent/output.py`, and for the same
reason. A model that hedges is telling us something we cannot otherwise see. A
model that is sure is telling us nothing, because a model asserting its own
reliability is the one claim it has no standing to make.

## What the bands mean

    HIGH          several independent, fresh, trusted signals agree, and where
                  an external system could confirm, it did
    MEDIUM        the evidence supports the finding but is thin, stale, or
                  drawn from one source
    LOW           evidence exists and does not clearly support the finding
    INSUFFICIENT  there is not enough evidence to have a view at all

INSUFFICIENT is deliberately distinct from LOW. "We looked and are unconvinced"
and "we do not have enough to look at" lead to different next actions, and
collapsing them is the same mistake as collapsing FAILED into UNKNOWN
(MerchantOps §53).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class Confidence(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


# Ordered weakest to strongest, so a band can be compared and capped.
_ORDER = (Confidence.INSUFFICIENT, Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)


def _rank(band: Confidence) -> int:
    return _ORDER.index(band)


def cap(band: Confidence, ceiling: Confidence) -> Confidence:
    """The weaker of two bands."""
    return band if _rank(band) <= _rank(ceiling) else ceiling


# Evidence older than this is stale: it may still be true, but it is no longer
# a statement about now. One hour is the window the detection rules already
# reason over, so a baseline drawn outside it is describing a different period.
FRESHNESS_WINDOW = timedelta(hours=1)

# Below this many independent sources, v2 §18's argument applies: one signal is
# an anomaly, several agreeing are an incident.
INDEPENDENT_SIGNALS_FOR_HIGH = 3


@dataclass
class ConfidenceAssessment:
    """The band, and every input that produced it.

    The inputs are kept because a band with no derivation is exactly the opaque
    number this module exists to replace. A merchant asking "why HIGH?" gets
    this, and so does an evaluator grading whether the model talked its way up.
    """
    band: Confidence
    # v2 §33's six inputs, each as a plain observation.
    total_evidence: int = 0
    trusted_evidence: int = 0
    independent_sources: int = 0
    agreeing_signals: int = 0
    stale_evidence: int = 0
    provider_confirmed: bool = False
    # v2 §18: other detection rules that saw the same episode.
    corroborating_rules: int = 0
    failed_tool_calls: int = 0
    model_confidence: float | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "band": self.band.value,
            "total_evidence": self.total_evidence,
            "trusted_evidence": self.trusted_evidence,
            "independent_sources": self.independent_sources,
            "agreeing_signals": self.agreeing_signals,
            "stale_evidence": self.stale_evidence,
            "provider_confirmed": self.provider_confirmed,
            "corroborating_rules": self.corroborating_rules,
            "failed_tool_calls": self.failed_tool_calls,
            "model_confidence": self.model_confidence,
            "reasons": self.reasons,
        }


def _model_ceiling(model_confidence: float | None) -> Confidence:
    """What the model's own number permits, as a ceiling only.

    A model that says 0.2 has volunteered a doubt it did not have to volunteer,
    and that is worth acting on. A model that says 0.99 has said nothing we can
    check, so it buys nothing — the ceiling for a confident model is simply
    HIGH, which constrains nothing.
    """
    if model_confidence is None:
        return Confidence.HIGH
    if model_confidence < 0.3:
        return Confidence.LOW
    if model_confidence < 0.6:
        return Confidence.MEDIUM
    return Confidence.HIGH


# Evidence sources that did not originate inside this system. v2 §33 names
# "provider confirmation" as an input precisely because it is the one kind of
# corroboration we cannot have produced ourselves by reasoning in a circle.
EXTERNAL_SOURCES = frozenset({"razorpay", "webhook_events"})


def assess(*, evidence: list, tool_calls: list | None = None,
           model_confidence: float | None = None,
           provider_confirmed: bool | None = None,
           corroborating_rules: int = 0,
           now: datetime | None = None) -> ConfidenceAssessment:
    """Compute the band from evidence. MerchantOps v2 §33.

    `evidence` is any sequence of objects with `source`, `untrusted` and
    `created_at` — `IncidentEvidence` rows in practice, but the signature is
    structural so a caller can assess a candidate finding before storing it.

    Untrusted evidence is counted but never *supports*. MerchantOps §39 says
    customer and order free text is data, not instruction; it is equally not
    corroboration, or an attacker who can write an order note can raise the
    system's stated confidence in a conclusion of their choosing.

    `provider_confirmed` defaults to being derived from the evidence's own
    sources rather than asked of the caller. A caller that has to remember to
    pass it is a caller that will eventually pass True by habit, and this is
    the one input that can lift a band.
    """
    now = now or datetime.now(timezone.utc)
    tool_calls = tool_calls or []
    a = ConfidenceAssessment(band=Confidence.INSUFFICIENT,
                             model_confidence=model_confidence)

    a.total_evidence = len(evidence)
    trusted = [e for e in evidence if not getattr(e, "untrusted", False)]
    a.trusted_evidence = len(trusted)
    a.independent_sources = len({getattr(e, "source", None) for e in trusted
                                 if getattr(e, "source", None)})
    if provider_confirmed is None:
        provider_confirmed = any(getattr(e, "source", None) in EXTERNAL_SOURCES
                                 for e in trusted)
    a.provider_confirmed = provider_confirmed

    # v2 §18's multivariate signal, arriving from `app.detection.correlation`:
    # other detection rules that independently saw the same episode. Counted
    # alongside evidence sources because that is what it is — an observation
    # this incident's own evidence does not contain. `corroborating_rules` is
    # the count of OTHER rules, so it adds directly.
    a.corroborating_rules = corroborating_rules
    a.independent_sources += corroborating_rules

    for e in trusted:
        created = getattr(e, "created_at", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now - created > FRESHNESS_WINDOW:
            a.stale_evidence += 1

    a.agreeing_signals = a.trusted_evidence - a.stale_evidence
    a.failed_tool_calls = sum(1 for t in tool_calls
                              if not getattr(t, "success", True))

    # ---------------------------------------------------------------- bands
    if a.trusted_evidence == 0:
        a.reasons.append(
            "no trusted evidence" if a.total_evidence == 0
            else f"all {a.total_evidence} evidence items are untrusted")
        a.band = Confidence.INSUFFICIENT
        return a

    if a.agreeing_signals == 0:
        a.reasons.append(f"all {a.trusted_evidence} trusted items are older than "
                         f"{int(FRESHNESS_WINDOW.total_seconds() // 60)} minutes")
        a.band = Confidence.LOW
    elif (a.independent_sources >= INDEPENDENT_SIGNALS_FOR_HIGH
          and a.stale_evidence == 0 and a.failed_tool_calls == 0):
        a.reasons.append(f"{a.independent_sources} independent sources agree, "
                         f"all within the freshness window")
        a.band = Confidence.HIGH
    elif a.independent_sources >= 2:
        a.reasons.append(f"{a.independent_sources} independent sources, "
                         f"{a.stale_evidence} stale, "
                         f"{a.failed_tool_calls} failed tool calls")
        a.band = Confidence.MEDIUM
    else:
        a.reasons.append("evidence comes from a single source")
        a.band = Confidence.LOW

    # Provider confirmation is the strongest single input v2 §33 names: it is
    # the one signal that did not originate inside this system. It can lift
    # MEDIUM to HIGH, and deliberately cannot rescue LOW -- confirming one fact
    # externally does not make a thin case a broad one.
    if provider_confirmed and a.band is Confidence.MEDIUM:
        a.reasons.append("externally confirmed by the provider")
        a.band = Confidence.HIGH

    # A tool that failed is a question that went unanswered.
    if a.failed_tool_calls and a.band is Confidence.HIGH:
        a.reasons.append(f"{a.failed_tool_calls} tool calls failed")
        a.band = Confidence.MEDIUM

    ceiling = _model_ceiling(model_confidence)
    if _rank(ceiling) < _rank(a.band):
        a.reasons.append(f"capped at {ceiling.value}: the model reported "
                         f"{model_confidence:.2f}")
        a.band = ceiling

    return a
