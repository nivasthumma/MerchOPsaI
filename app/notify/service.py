"""The send path: decide, record, deliver, record again.

The order matters and is the whole point of this module.

**Recorded before attempted.** A row is written and flushed before a socket is
opened. A process killed mid-send therefore leaves a PENDING row with an
attempt count, which somebody can find and act on. The alternative -- send
first, record after -- produces a notification that was delivered with no
record, which is indistinguishable from one never sent. This package exists
because approvals were expiring with nobody told and no way to find out; a send
path that can lose its own history rebuilds that problem one level down.

**Deduplicated by the database, not by an if.** The expiry sweep runs on a
cadence and the approval window is fifteen minutes, so the same "expiring soon"
is computed on every pass. `dedupe_key` is UNIQUE and the INSERT is what
decides; the preceding SELECT is only an optimisation. Two drains running
concurrently cannot both send.

**A channel that fails does not lose the notification.** The row goes to FAILED
with the error, and stays. `pending_notifications` finds them again.

Nothing here raises for a delivery failure. A notification is a side effect of
somebody else's work -- an approval being created, an incident opening -- and
taking that work down because SMTP was briefly unreachable would be trading a
missed email for a failed refund.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit.trace import current_correlation_id
from app.config import get_settings
from app.models import (
    NotificationKind,
    NotificationStatus,
    OperatorNotification,
)
from app.notify.channels import DeliveryRefused, UnconfiguredChannel, build_channels
from app.notify.channels import Message as ChannelMessage
from app.notify.messages import Rendered
from app.notify.routing import Recipient
from app.observability.logs import get_logger

log = get_logger("merchantops.notify")

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


@dataclass(frozen=True)
class SendReport:
    created: int
    sent: int
    failed: int
    suppressed: int
    duplicate: int

    def as_dict(self) -> dict:
        return {"created": self.created, "sent": self.sent, "failed": self.failed,
                "suppressed": self.suppressed, "duplicate": self.duplicate}


def configured_channels() -> list[str]:
    """The channels `notify_channels` names, validated against what exists.

    Raises rather than falling back. A deployment that lists `email` and has no
    SMTP host has made a mistake worth finding at startup, and the failure mode
    of quietly using `log` instead is somebody believing they are being emailed.
    """
    available = build_channels()
    wanted = [c.strip() for c in (get_settings().notify_channels or "").split(",") if c.strip()]
    if not wanted:
        wanted = ["log"]
    missing = [c for c in wanted if c not in available]
    if missing:
        raise UnconfiguredChannel(
            f"notify_channels names {missing}, which this deployment has not "
            f"configured. Available: {sorted(available)}. Either configure them "
            f"(SMTP_HOST/NOTIFY_EMAIL_FROM, SLACK_WEBHOOK_URL, NOTIFY_WEBHOOK_URL) "
            f"or remove them from NOTIFY_CHANNELS."
        )
    return wanted


def _below_threshold(severity: str) -> bool:
    floor = _SEVERITY_ORDER.get(get_settings().notify_min_severity, 0)
    return _SEVERITY_ORDER.get(severity, 0) < floor


def dedupe_key(kind: NotificationKind, subject_id: str, recipient: str,
               channel: str, *, bucket: str = "") -> str:
    """What makes two notifications the same one.

    `bucket` is how a recurring notification about the same subject stays
    sendable: the expiry sweep passes nothing (one chase per approval, ever),
    but a caller that genuinely wants one per day passes the date. Left to the
    caller because "the same" is a question about the notification, not about
    this function.
    """
    return f"{kind.value}|{subject_id}|{recipient}|{channel}|{bucket}"


def notify(session, *, kind: NotificationKind, tenant_id: str, merchant_id: str,
           subject_type: str, subject_id: str, rendered: Rendered,
           recipients: list[Recipient], bucket: str = "") -> SendReport:
    """Tell these people this thing, once each, on every configured channel."""
    if _below_threshold(rendered.severity):
        return SendReport(0, 0, 0, 0, 0)

    if not recipients:
        # Not an error, and not silent. A notification with nobody to send it to
        # means the routing rule found no one holding the required permission --
        # which is itself worth seeing, because it means an approval is waiting
        # that nobody on this merchant can grant.
        log.warning("notification_unroutable", extra={"notification": {
            "kind": kind.value, "subject": f"{subject_type}:{subject_id}",
            "merchant_id": merchant_id}})
        return SendReport(0, 0, 0, 0, 0)

    channels = build_channels()
    created = sent = failed = suppressed = duplicate = 0

    for name in configured_channels():
        channel = channels[name]
        for who in recipients:
            key = dedupe_key(kind, subject_id, who.email, name, bucket=bucket)
            row = OperatorNotification(
                id=f"NTF_{uuid.uuid4().hex[:16].upper()}",
                tenant_id=tenant_id, merchant_id=merchant_id,
                kind=kind, severity=rendered.severity,
                subject_type=subject_type, subject_id=subject_id,
                recipient=who.email, recipient_user_id=who.user_id, channel=name,
                title=rendered.title, body=rendered.body,
                status=NotificationStatus.PENDING,
                correlation_id=current_correlation_id(),
                dedupe_key=key,
            )
            # A SAVEPOINT, so a duplicate is a no-op rather than poisoning the
            # caller's transaction -- which on this path is somebody's approval
            # being created.
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                duplicate += 1
                continue

            created += 1
            outcome = _deliver(channel, row)
            sent += outcome == NotificationStatus.SENT
            failed += outcome == NotificationStatus.FAILED
            suppressed += outcome == NotificationStatus.SUPPRESSED

    session.flush()
    return SendReport(created, sent, failed, suppressed, duplicate)


def _deliver(channel, row: OperatorNotification) -> NotificationStatus:
    """One attempt on one channel. Never raises."""
    row.attempts += 1
    message = ChannelMessage(
        recipient=row.recipient, title=row.title, body=row.body,
        severity=row.severity, kind=row.kind.value,
        subject_type=row.subject_type, subject_id=row.subject_id,
        correlation_id=row.correlation_id,
    )
    try:
        channel.send(message)
    except DeliveryRefused as exc:
        row.status = NotificationStatus.SUPPRESSED
        row.last_error = str(exc)[:500]
    except Exception as exc:
        row.status = NotificationStatus.FAILED
        row.last_error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("notification_failed", extra={"notification": {
            "id": row.id, "channel": row.channel, "kind": row.kind.value,
            "error": row.last_error}})
    else:
        row.status = NotificationStatus.SENT
        row.sent_at = datetime.now(UTC)
    return row.status


def pending_notifications(session, *, merchant_id: str | None = None,
                          limit: int = 200) -> list[OperatorNotification]:
    """Notifications that were recorded and never got out.

    PENDING means the process died between the record and the send; FAILED
    means a channel refused and it is worth retrying. Both are "somebody was
    not told", which is the only reason this table is queryable.
    """
    q = select(OperatorNotification).where(
        OperatorNotification.status.in_(
            [NotificationStatus.PENDING, NotificationStatus.FAILED])
    ).order_by(OperatorNotification.created_at).limit(limit)
    if merchant_id:
        q = q.where(OperatorNotification.merchant_id == merchant_id)
    return list(session.execute(q).scalars().all())


def retry_pending(session, *, limit: int = 200) -> SendReport:
    """Re-attempt everything that did not get out. Idempotent by construction:
    a row that succeeds moves to SENT and is not selected again."""
    channels = build_channels()
    sent = failed = suppressed = 0
    for row in pending_notifications(session, limit=limit):
        channel = channels.get(row.channel)
        if channel is None:
            # The channel it was queued for no longer exists. Suppressed rather
            # than failed: nothing is wrong with the notification, and retrying
            # it forever against a channel that was removed is noise.
            row.status = NotificationStatus.SUPPRESSED
            row.last_error = f"channel {row.channel!r} is no longer configured"
            suppressed += 1
            continue
        outcome = _deliver(channel, row)
        sent += outcome == NotificationStatus.SENT
        failed += outcome == NotificationStatus.FAILED
        suppressed += outcome == NotificationStatus.SUPPRESSED
    session.flush()
    return SendReport(0, sent, failed, suppressed, 0)


class MisconfiguredNotifications(ValueError):
    """Settings that would produce a notification system that appears to work."""


def check_configuration() -> None:
    """Refuse settings whose only symptom is a notification arriving too late.

    `configured_channels` already catches naming a channel that does not exist.
    This catches the quieter one: a warning window wider than the thing it warns
    about. With `notify_approval_warning_seconds` >= `approval_ttl_seconds`,
    every approval is inside the window from the instant it is created, so the
    chase fires immediately alongside the request -- two notifications saying
    different things about the same approval in the same second, and no warning
    when the deadline actually approaches.

    Raised at startup rather than logged, because the failure it prevents is
    silent by construction: everything sends, nothing errors, and the only
    evidence is a chase that arrives at the wrong moment.
    """
    s = get_settings()
    if s.notify_approval_warning_seconds >= s.approval_ttl_seconds:
        raise MisconfiguredNotifications(
            f"NOTIFY_APPROVAL_WARNING_SECONDS ({s.notify_approval_warning_seconds}) "
            f"must be less than APPROVAL_TTL_SECONDS ({s.approval_ttl_seconds}). "
            f"As set, every approval is 'expiring' from the moment it is raised."
        )
    if s.notify_min_severity not in _SEVERITY_ORDER:
        raise MisconfiguredNotifications(
            f"NOTIFY_MIN_SEVERITY={s.notify_min_severity!r} is not one of "
            f"{sorted(_SEVERITY_ORDER)}. As set, nothing matches it and no "
            f"notification is ever sent."
        )
    configured_channels()
