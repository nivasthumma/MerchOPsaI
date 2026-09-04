"""Event-spine consumers that turn things happening into people being told.

These are ordinary `app.events.bus` consumers, registered on the same drain
every other consumer uses. That buys three properties without writing them:
the notification commits with the business write that caused it, a consumer
that throws is retried three times and then visible as DEAD rather than lost,
and a slow channel cannot hold up the request that raised the event.

**The event says which; the database says what.** Each consumer takes the
subject id off the event and loads the row. Event payloads are shaped by
whoever called `record()` and change when that call site changes; the table is
the state. Reading it also means the notification describes what was actually
committed rather than what was intended a moment earlier.

Not every §62 event has a consumer here, and most never will. §62 is a list of
things worth drawing on a timeline; this is a list of things worth interrupting
somebody for. `tool.started` is the former and emphatically not the latter.
"""
from __future__ import annotations

from sqlalchemy import text

from app.models import (
    Approval,
    Incident,
    NotificationKind,
)
from app.notify import messages
from app.notify.routing import recipients_for
from app.notify.service import notify
from app.observability.logs import get_logger

log = get_logger("merchantops.notify")


def _tenant_of(session, merchant_id: str | None) -> str | None:
    """The event carries `tenant_id` only when an incident was in scope, and an
    approval raised inside an agent run has none. Resolved from the merchant
    rather than left null, because routing checks both boundaries and a null
    tenant would make the outer one vacuous."""
    if not merchant_id:
        return None
    return session.execute(
        text("SELECT tenant_id FROM merchants WHERE id = :m"),
        {"m": merchant_id}).scalar()


# --------------------------------------------------------------------------
def on_approval_requested(session, event) -> None:
    """Somebody has to decide this, and the clock is already running.

    The single most load-bearing consumer in the package. Approvals expire on
    `approval_ttl_seconds` -- fifteen minutes by default -- and before this
    existed the only way to learn that one was waiting was to have the page
    open.
    """
    approval_id = (event.payload or {}).get("approval_id")
    if not approval_id:
        return
    ap = session.get(Approval, approval_id)
    if ap is None:                       # deleted, or a payload from an older shape
        return

    tenant_id = _tenant_of(session, ap.merchant_id)
    if tenant_id is None:
        return

    rendered = messages.approval(
        NotificationKind.APPROVAL_REQUESTED,
        approval_id=ap.id, action_type=ap.action_type, risk_level=ap.risk_level,
        payload=ap.action_payload or {}, expires_at=ap.expires_at, task_id=ap.task_id)

    notify(session, kind=NotificationKind.APPROVAL_REQUESTED,
           tenant_id=tenant_id, merchant_id=ap.merchant_id,
           subject_type="approval", subject_id=ap.id, rendered=rendered,
           recipients=recipients_for(session, NotificationKind.APPROVAL_REQUESTED,
                                     tenant_id=tenant_id, merchant_id=ap.merchant_id,
                                     action_type=ap.action_type))


# --------------------------------------------------------------------------
#: Below this, an incident is real but not worth an interruption. LOW and
#: MEDIUM incidents are work for the next time somebody looks; HIGH and
#: CRITICAL are the reason somebody would want to look now.
_INCIDENT_FLOOR = {"HIGH", "CRITICAL"}


def on_incident_created(session, event) -> None:
    incident_id = event.incident_id or (event.payload or {}).get("incident_id")
    if not incident_id:
        return
    inc = session.get(Incident, incident_id)
    if inc is None:
        return

    severity = getattr(inc.severity, "value", inc.severity)
    if severity not in _INCIDENT_FLOOR:
        return

    # `incidents` has no tenant_id column -- the merchant is the only link --
    # so this is resolved rather than read. Written as `getattr` nowhere on
    # purpose: reaching for an attribute that does not exist is how this was
    # wrong the first time.
    tenant_id = _tenant_of(session, inc.merchant_id)
    if tenant_id is None:
        return

    rendered = messages.incident(
        incident_id=inc.id,
        incident_type=getattr(inc.incident_type, "value", inc.incident_type),
        severity=severity,
        revenue_at_risk_minor=inc.revenue_at_risk_minor,
        detection_rule=inc.detection_rule or "unknown")

    notify(session, kind=NotificationKind.INCIDENT_OPENED,
           tenant_id=tenant_id, merchant_id=inc.merchant_id,
           subject_type="incident", subject_id=inc.id, rendered=rendered,
           recipients=recipients_for(session, NotificationKind.INCIDENT_OPENED,
                                     tenant_id=tenant_id, merchant_id=inc.merchant_id))


# --------------------------------------------------------------------------
def on_verification_completed(session, event) -> None:
    """Only UNKNOWN is news.

    SUCCESS and FAILED are both answers; UNKNOWN means we executed something
    against a payment provider and cannot say whether it took effect. That is
    the state MerchantOps treats as first-class and resolvable, and the one a
    human should know is open.
    """
    payload = event.payload or {}
    if str(payload.get("state") or payload.get("verification_state") or "").upper() != "UNKNOWN":
        return

    action_id = payload.get("action_id")
    if not action_id:
        return

    row = session.execute(text("""
        SELECT a.id, a.action_type, a.amount_minor, a.verify_attempts,
               a.task_id, a.merchant_id
        FROM agent_actions a WHERE a.id = :a
    """), {"a": action_id}).mappings().first()
    if row is None:
        return

    tenant_id = _tenant_of(session, row["merchant_id"])
    if tenant_id is None:
        return

    rendered = messages.unknown_verification(
        action_id=row["id"], action_type=row["action_type"],
        amount_minor=row["amount_minor"], attempts=row["verify_attempts"] or 0,
        task_id=row["task_id"])

    notify(session, kind=NotificationKind.VERIFICATION_UNKNOWN,
           tenant_id=tenant_id, merchant_id=row["merchant_id"],
           subject_type="action", subject_id=row["id"], rendered=rendered,
           recipients=recipients_for(session, NotificationKind.VERIFICATION_UNKNOWN,
                                     tenant_id=tenant_id, merchant_id=row["merchant_id"],
                                     action_type=row["action_type"]))


# --------------------------------------------------------------------------
_REGISTERED = False


def register() -> None:
    """Attach the consumers. Idempotent -- `subscribe` appends, so calling this
    twice would deliver every notification twice."""
    global _REGISTERED
    if _REGISTERED:
        return

    from app.events.bus import subscribe

    subscribe("approval.requested", on_approval_requested)
    subscribe("incident.created", on_incident_created)
    subscribe("verification.completed", on_verification_completed)
    _REGISTERED = True


def _reset_for_tests() -> None:
    """The registry is a module global and the suite builds many sessions."""
    global _REGISTERED
    _REGISTERED = False
