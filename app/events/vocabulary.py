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

# ---- taxonomy (ADR-0053) ----------------------------------------------------
# EVENT_TYPES above is the UI contract and stays exactly v2 §62's list. The
# other kinds of event this system raises are named separately, so the stream
# a browser renders is never widened by accident into "something happened".
#
#   DOMAIN        a business fact about money, written in the same transaction
#                 as the mutation it describes
#   INTEGRATION   what a provider told us -- evidence, never authority
#   UI            a timeline frame for a person watching (§62)
#   NOTIFICATION  somebody needs to be told (consumed by app.notify)
DOMAIN = "DOMAIN"
INTEGRATION = "INTEGRATION"
UI = "UI"
NOTIFICATION = "NOTIFICATION"

DOMAIN_EVENT_TYPES: tuple[str, ...] = (
    "refund.requested",      # an approved refund was reserved; nothing sent yet
    "refund.submitted",      # the provider accepted it and issued a reference
    "refund.verified",       # an independent read of the payment confirmed it
    "recovery.completed",    # every eligible candidate of a plan settled
)

#: One per provider event type we act on (app/webhooks/razorpay.py ACTIONABLE).
INTEGRATION_EVENT_TYPES: tuple[str, ...] = tuple(f"razorpay.{t}" for t in (
    "refund.processed", "refund.failed", "refund.created",
    "payment.captured", "payment.failed",
    "payment_link.paid", "payment_link.expired", "payment_link.cancelled",
))

_CATEGORY: dict[str, str] = {
    **{t: UI for t in EVENT_TYPES},
    # Shown on the timeline AND what the notify consumers act on; it is a
    # request for a person's decision before it is a frame.
    "approval.requested": NOTIFICATION,
    **{t: DOMAIN for t in DOMAIN_EVENT_TYPES},
    **{t: INTEGRATION for t in INTEGRATION_EVENT_TYPES},
}

#: What a browser's timeline receives: the §62 frames and nothing else.
STREAM_CATEGORIES: tuple[str, ...] = (UI, NOTIFICATION)

_KNOWN = frozenset(_CATEGORY)


def category_of(event_type: str) -> str | None:
    return _CATEGORY.get(event_type)


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
