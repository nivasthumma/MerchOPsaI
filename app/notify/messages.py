"""What a notification says.

Rendering is separate from sending so that the words are testable without a
socket, and separate from routing so that changing who is told does not change
what they read.

Three rules the content follows:

**Money is written the way the reader holds it.** Everything inside this system
is paise -- `amount_minor` -- because integers do not drift. Nobody approves
"₹4500000 paise"; they approve ₹45,000.00. The conversion happens here, once,
at the edge where a human reads it.

**The deadline is the point.** An approval has a fifteen-minute default TTL
(`approval_ttl_seconds`). A message that says "an approval is waiting" and not
"it expires at 14:32, in 11 minutes" is a message that gets read after the
window closes. Every approval notification leads with the clock.

**Nothing unredacted leaves.** Bodies are built from `redact`ed payloads on the
same rules as the audit trail, because an email is a wider audience than an
audit table and cannot be recalled once sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.audit.trace import redact
from app.config import get_settings
from app.models import NotificationKind


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    severity: str


def rupees(amount_minor: int | None) -> str:
    """Paise to a figure somebody recognises. Indian digit grouping, because the
    reader is an Indian merchant and 45,00,000 is not how they read ₹4.5m."""
    if amount_minor is None:
        return "unknown amount"
    whole, paise = divmod(int(amount_minor), 100)
    s = str(abs(whole))
    if len(s) > 3:                      # 12,34,567 -- last three, then pairs
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join([*groups, tail])
    sign = "-" if whole < 0 else ""
    return f"₹{sign}{s}.{paise:02d}"


def _link(path: str) -> str:
    base = (get_settings().notify_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def _countdown(expires_at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    seconds = int((expires_at - now).total_seconds())
    if seconds <= 0:
        return "already expired"
    if seconds < 90:
        return f"in {seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    return f"in {minutes // 60} hour{'s' if minutes // 60 != 1 else ''}"


# --------------------------------------------------------------------------
def approval(kind: NotificationKind, *, approval_id: str, action_type: str,
             risk_level: str, payload: dict, expires_at: datetime,
             task_id: str, now: datetime | None = None) -> Rendered:
    safe = redact(payload or {})
    amount = safe.get("amount_minor")
    target = safe.get("synthetic_payment_id") or safe.get("payment_id") or "—"

    if kind is NotificationKind.APPROVAL_EXPIRED:
        return Rendered(
            title=f"Approval expired without a decision — {action_type} {rupees(amount)}",
            severity="WARNING",
            body=(
                f"An approval expired before anybody decided it.\n\n"
                f"  action     {action_type}\n"
                f"  amount     {rupees(amount)}\n"
                f"  payment    {target}\n"
                f"  risk       {risk_level}\n"
                f"  expired    {expires_at.isoformat()}\n\n"
                f"Nothing was executed. The recovery this would have made did not "
                f"happen, and the task is closed as APPROVAL_EXPIRED.\n\n"
                f"{_link(f'/tasks/{task_id}')}\n"
            ))

    urgent = kind is NotificationKind.APPROVAL_EXPIRING
    when = _countdown(expires_at, now)
    dual = ("Two approvers are required at CRITICAL risk.\n"
            if risk_level == "CRITICAL" else "")
    return Rendered(
        title=(f"{'Expiring: ' if urgent else ''}Approval needed — "
               f"{action_type} {rupees(amount)} ({risk_level})"),
        # A CRITICAL action needs two signatures and has minutes to get them.
        severity="CRITICAL" if (urgent or risk_level == "CRITICAL") else "WARNING",
        body=(
            f"{'This approval expires ' + when + '.' if urgent else 'An action is waiting for your approval.'}\n\n"
            f"  action     {action_type}\n"
            f"  amount     {rupees(amount)}\n"
            f"  payment    {target}\n"
            f"  risk       {risk_level}\n"
            f"  expires    {expires_at.isoformat()} ({when})\n\n"
            f"{dual}"
            f"If nobody decides before it expires, nothing is executed and the "
            f"recovery does not happen.\n\n"
            f"{_link(f'/approvals?approval={approval_id}')}\n"
        ))


def incident(*, incident_id: str, incident_type: str, severity: str,
             revenue_at_risk_minor: int | None, detection_rule: str) -> Rendered:
    return Rendered(
        title=f"{severity} incident — {incident_type}, {rupees(revenue_at_risk_minor)} at risk",
        severity="CRITICAL" if severity == "CRITICAL" else "WARNING",
        body=(
            f"An incident was opened by the detection sweep.\n\n"
            f"  type       {incident_type}\n"
            f"  severity   {severity}\n"
            f"  at risk    {rupees(revenue_at_risk_minor)}\n"
            f"  found by   {detection_rule}\n\n"
            f"Revenue at risk is computed from the affected payments, not estimated.\n\n"
            f"{_link(f'/incidents/{incident_id}')}\n"
        ))


def escalated(*, action_id: str, action_type: str, amount_minor: int | None,
              reason: str, task_id: str) -> Rendered:
    return Rendered(
        title=f"Action escalated — {action_type} {rupees(amount_minor)} needs a human",
        severity="CRITICAL",
        body=(
            f"An action could not be settled automatically and has been escalated.\n\n"
            f"  action     {action_type}\n"
            f"  amount     {rupees(amount_minor)}\n"
            f"  reason     {reason}\n\n"
            f"The reconciliation sweep has stopped retrying this one. Until somebody "
            f"resolves it, whether the money moved is genuinely unknown to us.\n\n"
            f"{_link(f'/tasks/{task_id}')}\n"
        ))


def unknown_verification(*, action_id: str, action_type: str,
                         amount_minor: int | None, attempts: int,
                         task_id: str) -> Rendered:
    return Rendered(
        title=f"Verification UNKNOWN — {action_type} {rupees(amount_minor)}",
        severity="WARNING",
        body=(
            f"An action was executed and the provider's state could not be read "
            f"back, so we do not know whether it took effect.\n\n"
            f"  action     {action_type}\n"
            f"  amount     {rupees(amount_minor)}\n"
            f"  attempts   {attempts}\n\n"
            f"UNKNOWN is a resolvable state, not a failure: the reconciliation "
            f"sweep will keep asking. This is a notification that it is open, not "
            f"that it is stuck.\n\n"
            f"{_link(f'/tasks/{task_id}')}\n"
        ))
