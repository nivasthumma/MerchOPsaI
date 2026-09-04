"""Notifications that nothing raises an event for.

Two of the six kinds have no moment. An approval expiring is the *absence* of a
decision, and nothing publishes an event when a deadline passes -- approvals in
this system expire lazily, staying PENDING in the table until somebody tries to
use one (`app/agent/approval.py`). An escalated action is the reconciliation
sweep having given up, which is a threshold crossed rather than a step taken.

So these are found by looking, on a cadence, exactly as reconciliation is. That
is the same trade-off the README already states about detection and
reconciliation: bounded by cadence rather than real time, and honest about it.
A fifteen-minute approval window wants a sweep running every couple of minutes;
run it hourly and the chase arrives after the thing it was chasing.

Every send here is deduplicated by `dedupe_key`, so running the sweep more often
costs queries and sends nothing twice. That is deliberate -- the safe way to
tune this is to run it more often, and a sweep whose cost of over-running is a
duplicate email is one nobody dares tune.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.config import get_settings
from app.models import NotificationKind
from app.notify import messages
from app.notify.routing import recipients_for
from app.notify.service import SendReport, notify
from app.verification.reconciler import escalated_actions


def _merge(a: SendReport, b: SendReport) -> SendReport:
    return SendReport(a.created + b.created, a.sent + b.sent, a.failed + b.failed,
                      a.suppressed + b.suppressed, a.duplicate + b.duplicate)


_EMPTY = SendReport(0, 0, 0, 0, 0)


def _approvals(session, *, where: str, params: dict) -> list[dict]:
    rows = session.execute(text(f"""
        SELECT a.id, a.task_id, a.merchant_id, a.action_type, a.action_payload,
               a.risk_level, a.expires_at, m.tenant_id
        FROM approvals a
        JOIN merchants m ON m.id = a.merchant_id
        WHERE a.decision = 'PENDING' AND {where}
        ORDER BY a.expires_at
    """), params).mappings().all()  # noqa: S608 -- `where` is one of two literals below
    return [dict(r) for r in rows]


def sweep_approvals(session, *, now: datetime | None = None) -> SendReport:
    """Chase approvals about to expire, and report the ones that did.

    Both come from the same table read apart, because they are different
    messages to the same people: one is "decide this now", the other is "nobody
    decided, and here is what did not happen". Sending only the first would
    leave an approval that quietly lapsed looking like one that was never
    raised.
    """
    now = now or datetime.now(UTC)
    warning = get_settings().notify_approval_warning_seconds
    report = _EMPTY

    expiring = _approvals(
        session,
        where="a.expires_at > :now AND a.expires_at <= :soon",
        params={"now": now, "soon": now + timedelta(seconds=warning)})
    expired = _approvals(
        session, where="a.expires_at <= :now", params={"now": now})

    for kind, rows in ((NotificationKind.APPROVAL_EXPIRING, expiring),
                       (NotificationKind.APPROVAL_EXPIRED, expired)):
        for row in rows:
            rendered = messages.approval(
                kind, approval_id=row["id"], action_type=row["action_type"],
                risk_level=row["risk_level"], payload=row["action_payload"] or {},
                expires_at=row["expires_at"], task_id=row["task_id"], now=now)
            report = _merge(report, notify(
                session, kind=kind,
                tenant_id=row["tenant_id"], merchant_id=row["merchant_id"],
                subject_type="approval", subject_id=row["id"], rendered=rendered,
                recipients=recipients_for(session, kind,
                                          tenant_id=row["tenant_id"],
                                          merchant_id=row["merchant_id"],
                                          action_type=row["action_type"])))
    return report


def sweep_escalated(session, *, max_attempts: int = 5) -> SendReport:
    """Tell somebody about the operator work queue.

    `escalated_actions` has always been queryable and its docstring says it
    "must not be silently empty-looking". It was: nothing read it unless a
    person opened the page. This is that queue arriving instead of waiting.
    """
    report = _EMPTY
    for action in escalated_actions(session, max_attempts=max_attempts):
        tenant_id = session.execute(
            text("SELECT tenant_id FROM merchants WHERE id = :m"),
            {"m": action["merchant_id"]}).scalar()
        if tenant_id is None:
            continue

        detail = action.get("verification_detail") or {}
        reason = detail.get("reason") or detail.get("error") or (
            f"{action['verify_attempts']} verification attempts, "
            f"state {action['verification_state'] or 'never established'}")

        rendered = messages.escalated(
            action_id=action["id"], action_type=action["action_type"],
            amount_minor=action["amount_minor"], reason=str(reason),
            task_id=action["task_id"])
        report = _merge(report, notify(
            session, kind=NotificationKind.ACTION_ESCALATED,
            tenant_id=tenant_id, merchant_id=action["merchant_id"],
            subject_type="action", subject_id=action["id"], rendered=rendered,
            recipients=recipients_for(session, NotificationKind.ACTION_ESCALATED,
                                      tenant_id=tenant_id,
                                      merchant_id=action["merchant_id"],
                                      action_type=action["action_type"])))
    return report


def sweep(session, *, now: datetime | None = None) -> dict:
    """Everything time-based, in one call. What a scheduler runs."""
    approvals = sweep_approvals(session, now=now)
    escalations = sweep_escalated(session)
    return {"approvals": approvals.as_dict(), "escalated": escalations.as_dict()}
