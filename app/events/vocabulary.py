"""The live event vocabulary — MerchantOps v2 §62.

v2 §62 lists fifteen events the UI should receive "so the merchant sees the
system operating in real time". Unlike §47's audit vocabulary — which the spec
itself calls "Examples", and which `app.audit.trace.CANONICAL_EVENT` therefore
maps loosely — this list is a contract with a client. A UI subscribing to
`tool.completed` and receiving `tool_completed` gets nothing, silently.

So these names are exact and closed. `publish` rejects anything not in the set,
because an event type nobody subscribes to is indistinguishable from a typo,
and the failure surfaces at the call site instead of as a timeline that is
quietly missing a step.

The audit trail and the event stream stay separate on purpose. Audit is the
durable record of what the application did and answers to §67; this is a
notification that it did it. They overlap in content and differ in obligation:
losing an audit row is a compliance failure, losing a stream frame is a stale
screen that the next poll corrects.
"""
from __future__ import annotations

# v2 §62, in the order the document lists them, which is also the order a
# single incident produces them.
EVENT_TYPES: tuple[str, ...] = (
    "incident.created",
    "agent.started",
    "tool.started",
    "tool.completed",
    "evidence.discovered",
    "hypothesis.created",
    "hypothesis.rejected",
    "recommendation.created",
    "policy.evaluated",
    "approval.requested",
    "action.started",
    "action.completed",
    "verification.started",
    "verification.completed",
    "incident.resolved",
)

_KNOWN = frozenset(EVENT_TYPES)


def is_known(event_type: str) -> bool:
    return event_type in _KNOWN


# Which v2 §62 event, if any, an existing snake_case audit event corresponds to.
#
# This is deliberately partial. Most audit events have no §62 counterpart —
# §62 is a list of things worth *showing*, not everything worth recording — and
# mapping one anyway would put frames on a merchant's timeline that mean
# nothing to them. Where the correspondence is real, this is what lets a single
# `record(...)` call also raise a stream frame without the caller naming both.
FROM_AUDIT_EVENT: dict[str, str] = {
    "incident_detected": "incident.created",
    "task_created": "agent.started",
    "tool_call": "tool.completed",
    "agent_output": "recommendation.created",
    "policy_decision": "policy.evaluated",
    "policy_recheck": "policy.evaluated",
    "approval_requested": "approval.requested",
    "action_executing": "action.started",
    "action_recorded": "action.completed",
    "verification": "verification.completed",
    "reverification": "verification.completed",
}
