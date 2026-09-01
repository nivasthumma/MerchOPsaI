"""The evidence graph — MerchantOps v2 §32.

§32 exists to answer one question a merchant can ask about any conclusion:

    "Why do you believe this?"

`app.evidence.graph` draws the answer as typed edges over rows that already
exist, and `explain` reads it back. Nothing here invents a relationship: an
edge is written by deterministic code from state the platform already owns.
"""
from app.evidence.graph import build, draw, edges_for, explain, why
from app.evidence.hypotheses import (
    adjudicate, for_incident, leading, propose,
)

__all__ = [
    "build", "draw", "edges_for", "explain", "why",
    "adjudicate", "propose", "leading", "for_incident",
]
