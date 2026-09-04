"""Telling a human that the system needs them.

Everything around this was built and this was not: approvals are created
server-side, expire on a timer, require two signatures at CRITICAL risk and are
re-checked at execution; incidents open with a computed severity; actions
escalate to a queue; UNKNOWN verifications wait for a person. And nothing in
the application sent anything to anybody. The only way to learn that a CRITICAL
refund was waiting on your signature was to have the page open when it
appeared -- against a fifteen-minute default expiry.

The parts, in the order a notification passes through them:

  routing.py    who gets told -- derived from the permissions the action
                requires, so the list cannot drift from what policy enforces
  messages.py   what it says -- paise rendered as rupees, the deadline first,
                redacted on the audit trail's rules
  channels.py   where it goes -- log (always), email, Slack, outbound webhook
  service.py    the send path -- recorded before attempted, deduplicated by a
                UNIQUE constraint rather than by an if
  consumers.py  the event-spine subscriptions: approval requested, incident
                opened, verification UNKNOWN
  sweep.py      the two kinds nothing raises an event for -- an approval about
                to expire, and an action reconciliation gave up on

What this does not do yet: there is no scheduler, so `sweep` needs a caller
(`scripts/notify_sweep.py`, or the route). That is the next piece of Phase 1,
and it is the same gap detection and reconciliation already have.
"""
from app.notify.consumers import register
from app.notify.service import (
    SendReport,
    check_configuration,
    notify,
    pending_notifications,
    retry_pending,
)
from app.notify.sweep import sweep, sweep_approvals, sweep_escalated

__all__ = [
    "SendReport",
    "check_configuration",
    "notify",
    "pending_notifications",
    "register",
    "retry_pending",
    "sweep",
    "sweep_approvals",
    "sweep_escalated",
]
