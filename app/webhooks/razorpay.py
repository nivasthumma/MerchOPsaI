"""Razorpay webhook ingestion — MerchantOps §11, §34, §35.

    delivery
      -> signature validation
      -> event id deduplication
      -> durable storage
      -> processing
      -> reconciliation

## The one idea this module is built around

**A webhook is evidence, not authority.** It is a message from a system we do
not control, delivered over a channel we do not control, and MerchantOps §32
already says a provider's HTTP 200 is not proof of business state. A pushed
message is weaker than that, not stronger.

So nothing here writes `agent_actions.verification_state` from a payload. A
relevant event marks an action for immediate re-verification, and
`reverify_action` goes and *reads provider state through the adapter*. The
webhook decides when to look; it never decides what was found.

That distinction is what makes forgery uninteresting. An attacker who defeats
the signature can make us re-read state we would have read anyway — they cannot
tell us what that state is.

## Why rejected deliveries are still stored

A webhook refused for a bad signature that leaves no row is an attack nobody can
investigate. Every delivery lands in `webhook_events` with the status it earned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import (
    ActionStatus, AgentAction, VerificationState, WebhookEvent, WebhookStatus,
)

SCHEMA_VERSION = "v1"

# Events this system acts on. Anything else is stored and marked IGNORED --
# recorded, because the event store is the record of what the provider said,
# but not routed anywhere.
ACTIONABLE = frozenset({
    "refund.processed", "refund.failed", "refund.created",
    "payment.captured", "payment.failed",
    # MerchantOps §49. A payment link becomes recovery when somebody PAYS it,
    # and nothing was listening for that — a paid link was discovered only when
    # a plan happened to be settled, so recovered revenue lagged reality by
    # however long it took someone to ask.
    "payment_link.paid", "payment_link.expired", "payment_link.cancelled",
})


@dataclass
class IngestResult:
    status: WebhookStatus
    event_id: str | None
    stored_id: str | None = None
    note: str = ""
    reverified: list[str] = None          # action ids re-read as a result
    incident_id: str | None = None        # raised on a state contradiction

    def as_dict(self) -> dict:
        return {
            "status": self.status.value, "event_id": self.event_id,
            "stored_id": self.stored_id, "note": self.note,
            "reverified": self.reverified or [], "incident_id": self.incident_id,
        }


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Razorpay signs the raw request body with HMAC-SHA256.

    `raw_body` must be the exact bytes received. Re-serialising parsed JSON
    produces different bytes -- different key order, different whitespace -- and
    the signature would never match. That is the classic way this check gets
    written so that it silently always fails, or worse, gets removed for
    "not working".
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Constant time: a plain == leaks the correct prefix through timing.
    return hmac.compare_digest(expected, signature)


def _extract(payload: dict) -> tuple[str, str | None, datetime | None]:
    """(event_type, entity_id, occurred_at) from a Razorpay event envelope."""
    event_type = payload.get("event") or "unknown"
    entity_id = None

    body = payload.get("payload") or {}
    # `contains` names which entities the event carries; prefer the refund when
    # both are present, because a refund event's payment entity is context.
    # payment_link first: a link event's `payment` entity is the payment that
    # settled it, and matching on that would reconcile the wrong action.
    for key in ("payment_link", "refund", "payment", "order"):
        ent = (body.get(key) or {}).get("entity") or {}
        if ent.get("id"):
            entity_id = ent["id"]
            break

    occurred_at = None
    if isinstance(payload.get("created_at"), (int, float)):
        occurred_at = datetime.fromtimestamp(payload["created_at"], tz=timezone.utc)
    return event_type, entity_id, occurred_at


def _owner_for_entity(session, entity_id: str | None) -> tuple[str | None, str | None]:
    """(tenant_id, merchant_id) from our own records, never from the payload.

    The event names a provider id; which merchant that belongs to is a fact this
    system already holds. Trusting an `account_id` in the body would let a
    forged delivery address another tenant (MerchantOps §54).
    """
    if not entity_id:
        return None, None
    merchant = session.execute(text("""
        SELECT merchant_id FROM payments WHERE external_payment_id = :e
        UNION
        SELECT merchant_id FROM agent_actions
         WHERE external_payment_id = :e OR external_reference = :e
        LIMIT 1
    """), {"e": entity_id}).scalar()
    if merchant is None:
        return None, None
    tenant = session.execute(
        text("SELECT tenant_id FROM merchants WHERE id = :m"), {"m": merchant}).scalar()
    return tenant, merchant


def ingest(session, raw_body: bytes, signature: str | None,
           event_id_header: str | None, adapter=None) -> IngestResult:
    """Take one delivery all the way through the pipeline."""
    settings = get_settings()
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    correlation_id = f"COR_{uuid.uuid4().hex[:12].upper()}"

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("event envelope must be an object")
    except (UnicodeDecodeError, ValueError) as exc:
        return IngestResult(WebhookStatus.INVALID, None,
                            note=f"Unparseable body: {exc}")

    event_type, entity_id, occurred_at = _extract(payload)
    # Razorpay carries the event id in a header. Falling back to the payload
    # hash keeps deduplication working when it is absent, at the cost of
    # treating two byte-identical deliveries as one -- which is what we want
    # anyway, and is stated in the note rather than hidden.
    event_id = event_id_header or f"sha256:{payload_hash}"

    # ---- signature (MerchantOps §34) ----------------------------------
    if not settings.webhook_verification_enabled:
        sig_ok, status, note = (
            False, WebhookStatus.IGNORED,
            "No webhook secret configured; the delivery is recorded but NOT "
            "acted on. An unverified event never changes state.")
    elif verify_signature(raw_body, signature, settings.razorpay_webhook_secret):
        sig_ok, status, note = True, WebhookStatus.RECEIVED, ""
    else:
        sig_ok, status, note = (
            False, WebhookStatus.INVALID,
            "Signature did not verify. Stored for investigation; not processed.")

    if not event_id_header:
        note = (note + " " if note else "") + "No event id header; deduplicating on payload hash."

    tenant_id, merchant_id = _owner_for_entity(session, entity_id) if sig_ok else (None, None)

    row = WebhookEvent(
        id=f"WHE_{uuid.uuid4().hex[:10].upper()}",
        event_id=event_id, provider="razorpay", event_type=event_type,
        schema_version=SCHEMA_VERSION, tenant_id=tenant_id, merchant_id=merchant_id,
        entity_id=entity_id,
        status=status, signature_valid=sig_ok, payload=payload,
        payload_hash=payload_hash, correlation_id=correlation_id,
        occurred_at=occurred_at, processing_note=note or None,
    )

    # ---- deduplication (MerchantOps §34) ------------------------------
    # Attempted, not pre-checked: a SELECT-then-INSERT is a race two concurrent
    # deliveries both lose. Providers retry by design, so this path is ordinary.
    sp = session.begin_nested()
    try:
        session.add(row)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        prior = session.execute(
            text("SELECT id FROM webhook_events WHERE event_id = :e"),
            {"e": event_id}).scalar()
        return IngestResult(WebhookStatus.DUPLICATE, event_id, stored_id=prior,
                            note="Already delivered; not processed again.")

    if status is not WebhookStatus.RECEIVED:
        return IngestResult(status, event_id, stored_id=row.id, note=note)

    # ---- processing ----------------------------------------------------
    if event_type not in ACTIONABLE:
        row.status = WebhookStatus.IGNORED
        row.processed_at = datetime.now(timezone.utc)
        row.processing_note = f"No subscriber for '{event_type}'."
        session.flush()
        return IngestResult(WebhookStatus.IGNORED, event_id, stored_id=row.id,
                            note=row.processing_note)

    from app.webhooks.processing import process_event
    return process_event(session, row, adapter=adapter)
