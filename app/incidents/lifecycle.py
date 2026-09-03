"""Incident lifecycle state machine — MerchantOps §13.

This is deterministic control-plane logic. The model may *investigate* an
incident and it may *recommend*; it can never move one. Nothing in this module
reads model output, and `transition()` takes no free text from a tool result.

## Why the transitions are explicit rather than derived

An earlier draft derived legality from position in the canonical chain: any
forward move is legal, any backward move is not. That is one line of code and
it is wrong in the one place this project cares about most. `UNKNOWN` sits
*after* `RESOLVED` in no ordering at all, and an incident parked in UNKNOWN
because its actions have not settled must be able to reach RESOLVED once
reconciliation settles them. An ordering rule either forbids that -- stranding
the incident, which is the exact failure UNKNOWN exists to prevent -- or admits
backward moves generally, which forbids nothing.

So the map is written out. It is longer, and it is checkable by reading.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models import Incident
from app.models import IncidentStatus as S

# Exception states are reachable from any live state (MerchantOps §13).
EXCEPTION = frozenset({S.FAILED, S.UNKNOWN, S.ESCALATED, S.CANCELLED})

# CLOSED is the only truly terminal state: nothing leaves it.
TERMINAL = frozenset({S.CLOSED})

# The canonical forward chain, §13. Skips are legal -- an incident whose
# recommendation is NO_ACTION never reaches APPROVAL_REQUIRED or EXECUTING and
# must still be able to resolve -- so each state lists every legal successor
# rather than only the next one.
_CANONICAL: dict[S, set[S]] = {
    S.DETECTED: {S.TRIAGED, S.INVESTIGATING},
    S.TRIAGED: {S.INVESTIGATING},
    S.INVESTIGATING: {S.ROOT_CAUSE_IDENTIFIED, S.RESOLVED},
    S.ROOT_CAUSE_IDENTIFIED: {S.RECOVERY_PLANNED, S.RESOLVED},
    S.RECOVERY_PLANNED: {S.POLICY_EVALUATING, S.RESOLVED},
    S.POLICY_EVALUATING: {S.APPROVAL_REQUIRED, S.EXECUTING, S.RESOLVED},
    S.APPROVAL_REQUIRED: {S.EXECUTING, S.CANCELLED},
    S.EXECUTING: {S.VERIFYING},
    S.VERIFYING: {S.RESOLVED},
    S.RESOLVED: {S.CLOSED},
}

# Exception-state exits. UNKNOWN is deliberately not a dead end: it is the
# incident-level mirror of the action-level UNKNOWN exit path, and an incident
# that can never leave UNKNOWN would convert an unsettled state into a
# permanent one (MerchantOps §33).
_FROM_EXCEPTION: dict[S, set[S]] = {
    S.UNKNOWN: {S.VERIFYING, S.RESOLVED, S.ESCALATED, S.CLOSED},
    S.FAILED: {S.ESCALATED, S.CLOSED},
    S.ESCALATED: {S.CLOSED},
    S.CANCELLED: {S.CLOSED},
}


class IllegalTransition(Exception):
    """A transition the state machine refuses. Never caught and downgraded:
    an incident that moved illegally is a control-plane defect, not a warning."""

    def __init__(self, incident_id: str, frm: S, to: S):
        self.incident_id, self.frm, self.to = incident_id, frm, to
        super().__init__(
            f"Incident {incident_id} cannot move {frm.value} -> {to.value}. "
            f"Legal from {frm.value}: {sorted(x.value for x in legal_from(frm)) or 'none (terminal)'}."
        )


def legal_from(state: S) -> set[S]:
    """Every state reachable from `state` in one step."""
    if state in TERMINAL:
        return set()
    if state in EXCEPTION:
        return set(_FROM_EXCEPTION.get(state, set()))
    # A live canonical state may always fail, escalate, cancel or go unknown.
    return set(_CANONICAL.get(state, set())) | set(EXCEPTION)


def is_legal(frm: S, to: S) -> bool:
    return to in legal_from(frm)


def transition(session, incident: Incident, to: S, *, reason: str,
               actor: str = "system", payload: dict | None = None) -> Incident:
    """Move an incident, or refuse. Records the move on the audit trail.

    `actor` is recorded but never consulted: authority to move an incident is
    a property of the calling code path, not of a string passed into it.
    """
    # Imported here: app.audit.trace imports app.models, and a module-level
    # import would close the cycle through app.incidents.
    from app.audit.trace import record_incident

    frm = incident.status
    if not is_legal(frm, to):
        raise IllegalTransition(incident.id, frm, to)

    incident.status = to
    if to is S.RESOLVED and incident.resolved_at is None:
        incident.resolved_at = datetime.now(UTC)
    session.flush()

    record_incident(session, incident, "incident_status_changed", {
        "from": frm.value, "to": to.value, "reason": reason, "actor": actor,
        **(payload or {}),
    })
    return incident
