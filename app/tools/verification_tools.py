"""Verification and reconciliation tools — MerchantOps §18, §32, §35.

These are the model's read access to *provider* state, as distinct from the
investigation tools which read our own records. The distinction is the point of
§32: internal state is what we believe, provider state is what happened, and a
model reasoning about a financial outcome should be able to tell which it is
looking at.

`reconcile_transaction` is the only one that writes, and what it writes is the
result of a read. It can never create a financial effect: it calls the same
`reverify_action` the sweep and the webhook path use, which re-reads the payment
and records what it found. That is why it is LOW risk despite changing state --
the same reason `/tasks/{id}/reverify` needs no approval.
"""
from __future__ import annotations

from sqlalchemy import text

from app.models import AgentAction, WebhookEvent
from app.tools.contracts import Evidence, RiskClass, ToolResult, ToolSpec

SPEC_PAYMENT_STATUS = ToolSpec(
    name="get_payment_status",
    description=(
        "Read a payment's CURRENT state from the payment provider, not from our "
        "records. Use this to check what actually happened externally, and to "
        "compare it against what our own data says."
    ),
    input_schema={
        "type": "object",
        "properties": {"payment_id": {"type": "string",
                                      "description": "Internal payment id, e.g. SYN_PAY_0002."}},
        "required": ["payment_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def get_payment_status(session, merchant_id: str, payment_id: str, *, adapter=None) -> ToolResult:
    from app.integrations.razorpay.adapter import get_adapter

    row = session.execute(text("""
        SELECT external_payment_id, amount_minor, amount_refunded_minor, status
        FROM payments WHERE id = :p AND merchant_id = :m
    """), {"p": payment_id, "m": merchant_id}).mappings().first()
    if row is None:
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"payment_id": payment_id}, risk_level="LOW")
    if not row["external_payment_id"]:
        return ToolResult(success=False, error_code="TOOL_INVALID_ARGUMENT",
                          data={"error": "not_externally_mapped",
                                "detail": f"{payment_id} has no provider mapping; there "
                                          f"is no external state to read."},
                          risk_level="LOW")

    adapter = adapter or get_adapter(session)
    try:
        ext = adapter.get_payment(row["external_payment_id"])
    except Exception as exc:                                        # noqa: BLE001
        # A failed read is reported as a failed read. Falling back to our own
        # records here would answer a question about provider state with
        # internal state, which is the substitution §32 exists to prevent.
        return ToolResult(success=False, error_code="EXTERNAL_API_ERROR",
                          data={"error": str(exc)[:200]}, risk_level="LOW")
    if ext is None:
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"error": "provider has no such payment"}, risk_level="LOW")

    internal_refunded = int(row["amount_refunded_minor"])
    agrees = ext.amount_refunded_minor == internal_refunded
    data = {
        "payment_id": payment_id, "external_payment_id": ext.id,
        "provider_status": ext.status,
        "provider_amount_minor": ext.amount_minor,
        "provider_amount_refunded_minor": ext.amount_refunded_minor,
        "provider_refund_status": ext.refund_status,
        "internal_status": row["status"],
        "internal_amount_refunded_minor": internal_refunded,
        "internal_and_provider_agree": agrees,
    }
    return ToolResult(
        success=True, data=data, external_reference=ext.id, risk_level="LOW",
        evidence=[
            Evidence(key="provider_status", value=ext.status, source="razorpay"),
            Evidence(key="provider_amount_refunded",
                     value=f"INR {ext.amount_refunded_minor / 100:,.2f}", source="razorpay"),
            Evidence(key="internal_and_provider_agree", value=agrees, source="verification"),
        ])


SPEC_PROVIDER_EVENT = ToolSpec(
    name="get_provider_event",
    description=(
        "Provider webhook events recorded for an entity: what the provider told "
        "us, when, and whether we acted on it. This is evidence of what was "
        "SAID, not of what is true — use get_payment_status for current state."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entity_id": {"type": "string",
                          "description": "Provider entity id, e.g. pay_xxx or rfnd_xxx."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["entity_id", "limit"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def get_provider_event(session, merchant_id: str, entity_id: str, limit: int = 10) -> ToolResult:
    rows = (session.query(WebhookEvent)
            .filter(WebhookEvent.entity_id == entity_id,
                    WebhookEvent.merchant_id == merchant_id)
            .order_by(WebhookEvent.received_at.desc())
            .limit(min(limit, 50)).all())
    data = {
        "entity_id": entity_id, "event_count": len(rows),
        "events": [{
            "event_id": e.event_id, "event_type": e.event_type,
            "status": e.status.value, "signature_valid": e.signature_valid,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "received_at": e.received_at.isoformat(),
            "processed_at": e.processed_at.isoformat() if e.processed_at else None,
        } for e in rows],
    }
    ev = [Evidence(key="provider_event_count", value=len(rows), source="webhook_events")]
    ev += [Evidence(key=f"event_{e.event_id}",
                    value=f"{e.event_type} ({e.status.value})", source="webhook_events")
           for e in rows[:5]]
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


SPEC_RECONCILE = ToolSpec(
    name="reconcile_transaction",
    description=(
        "Re-read the provider's state for a previously executed action and "
        "record what was found. Use this to resolve an action whose outcome is "
        "UNKNOWN. This NEVER re-issues the action; it only reads."
    ),
    input_schema={
        "type": "object",
        "properties": {"action_id": {"type": "string", "description": "e.g. ACT_XXXXXXXX"}},
        "required": ["action_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def reconcile_transaction(session, merchant_id: str, action_id: str, *, adapter=None) -> ToolResult:
    from app.integrations.razorpay.adapter import get_adapter
    from app.tools.actions import reverify_action

    action = session.get(AgentAction, action_id)
    if action is None or action.merchant_id != merchant_id:
        # No distinction between absent and another merchant's (§54).
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"action_id": action_id}, risk_level="LOW")

    before = action.verification_state
    adapter = adapter or get_adapter(session)
    try:
        vr = reverify_action(session, adapter, action)
    except Exception as exc:                                        # noqa: BLE001
        # MerchantOps §57: an unreadable state is not a failed action. Leaving
        # it as it was is the correct outcome; the sweep will try again.
        return ToolResult(success=False, error_code="EXTERNAL_STATE_UNKNOWN",
                          data={"action_id": action_id, "error": str(exc)[:200],
                                "state": before.value if before else None},
                          risk_level="LOW")

    data = {
        "action_id": action.id, "action_type": action.action_type,
        "from": before.value if before else None, "to": vr.state.value,
        "settled": vr.state.value in ("SUCCESS", "FAILED"),
        "reason": vr.reason, "attempts": action.verify_attempts,
        "external_reference": action.external_reference,
    }
    return ToolResult(
        success=True, data=data, external_reference=action.external_reference,
        risk_level="LOW",
        evidence=[
            Evidence(key="verification_state", value=vr.state.value, source="verification"),
            Evidence(key="reconciliation", value=data["reason"], source="verification"),
        ])
