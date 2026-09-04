"""Who gets told.

There is one rule and it is not a new one: **a notification about an action
goes to the people who could perform that action**, and "could perform" is
`app.policy.engine.required_permissions` -- the same function the policy engine
gates on. Deriving it rather than keeping a second list is the point. A
recipient list maintained beside the permission model drifts from it, and the
direction it drifts in is always the same: somebody is on the list who can no
longer act, or can act and stopped being told.

Everything here is scoped to one merchant, and the scope is not optional. A
notification is the one artefact that leaves the system and cannot be recalled,
so a cross-merchant recipient is a data leak delivered by email. Both boundaries
are checked -- tenant outermost, then merchant -- in the same order every other
read in this system checks them.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import NotificationKind
from app.policy.engine import required_permissions


@dataclass(frozen=True)
class Recipient:
    user_id: str
    email: str
    role: str


def _users(session, *, tenant_id: str, merchant_id: str):
    """Everyone attached to this merchant, with what their role grants.

    Through `app.authz` rather than a query of its own: permissions live in
    tables now, and a second query that joins them its own way is a second
    answer to "what may this user do".
    """
    from app import authz

    return authz.holders(session, tenant_id=tenant_id, merchant_id=merchant_id)


def who_can_perform(session, *, tenant_id: str, merchant_id: str,
                    action_type: str) -> list[Recipient]:
    """Users holding every permission `action_type` requires.

    An action type the registry does not know requires nothing, which would
    otherwise mean "everybody". That is the wrong default for a notification
    about money, so an unknown action type routes to nobody and the caller
    records the notification as SUPPRESSED with that reason -- visible, rather
    than fanned out.
    """
    required = required_permissions(action_type)
    if not required:
        return []
    from app import authz

    return [Recipient(p.user_id, p.email, p.role) for p in authz.holders(
        session, tenant_id=tenant_id, merchant_id=merchant_id, required=required)]


def who_watches(session, *, tenant_id: str, merchant_id: str) -> list[Recipient]:
    """Everyone attached to the merchant.

    For things that are news rather than a request: an incident opened, a
    verification that came back UNKNOWN. There is no permission that means
    "should hear about this", and inventing one would be inventing a
    requirement the policy engine does not have.
    """
    return [Recipient(p.user_id, p.email, p.role)
            for p in _users(session, tenant_id=tenant_id, merchant_id=merchant_id)]


def recipients_for(session, kind: NotificationKind, *, tenant_id: str,
                   merchant_id: str, action_type: str | None = None) -> list[Recipient]:
    """The routing table, such as it is.

    Kept as one function so that "who is told about X" is answerable by reading
    one place, rather than by finding every call site.
    """
    needs_authority = {
        NotificationKind.APPROVAL_REQUESTED,
        NotificationKind.APPROVAL_EXPIRING,
        NotificationKind.APPROVAL_EXPIRED,
        # An escalated action and an UNKNOWN verification both end in somebody
        # deciding what to do about money that may or may not have moved. That
        # is the same authority as performing the action in the first place.
        NotificationKind.ACTION_ESCALATED,
        NotificationKind.VERIFICATION_UNKNOWN,
    }
    if kind in needs_authority:
        if action_type is None:
            return []
        return who_can_perform(session, tenant_id=tenant_id,
                               merchant_id=merchant_id, action_type=action_type)
    return who_watches(session, tenant_id=tenant_id, merchant_id=merchant_id)
