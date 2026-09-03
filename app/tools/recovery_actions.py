"""Payment-link and notification actions — MerchantOps §18, §29, §31, §32.

## Why these are not read tools

`generate_payment_link` and `send_customer_notification` change state outside
this system. A notification cannot be unsent; a payment link, once a customer
has it, has been given to them. Routing either through `execute_read_tool` would
have handed the model two un-idempotent external effects with no action record,
no idempotency key and no verification -- the three things that make the refund
path safe.

So they take the same route a refund takes:

    approval verified (server-side)
        -> derive idempotency key SERVER-SIDE (§31; never model-supplied)
        -> reserve agent_actions row (UNIQUE key claims the action)
        -> external call
        -> record external reference
        -> independent read-back (§32)

The only difference from a refund is what "verified" means, and each has its own
predicate below.

## Risk

Both are MEDIUM (§24: "Send notification -> MEDIUM"), which under §25 means a
human approves each one. That is strict, and deliberately so: no money moves,
but a customer is contacted, and this system's posture is that state-changing
effects on third parties get a human. A campaign that wants to send thirty-three
of them is bulk, which is CRITICAL, which escalates.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import checkpoint
from app.integrations.razorpay.faults import ProviderError, ProviderTimeout
from app.models import ActionStatus, AgentAction, VerificationState
from app.tools.contracts import Evidence, RiskClass, ToolResult, ToolSpec
from app.verification.engine import VerificationResult

SPEC_PAYMENT_LINK = ToolSpec(
    name="generate_payment_link",
    description=(
        "Create a payment link so a customer can complete a payment that failed. "
        "This CONTACTS THE CUSTOMER and requires human approval; it can never "
        "execute directly from this call. Give the failed payment's id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "synthetic_payment_id": {
                "type": "string",
                "description": "The FAILED payment to recover, e.g. SYN_PAY_0123.",
            },
            "reason": {"type": "string", "description": "Why a link is warranted."},
        },
        # amount is NOT model-supplied: it is the failed payment's own amount,
        # read from the database at execution time. A model-chosen amount would
        # be a model-chosen request for money.
        "required": ["synthetic_payment_id", "reason"],
    },
    required_permissions=["action:recover"],
    risk_class=RiskClass.MEDIUM,
    audit_required=True,
    idempotent=True,
    reversible=False,
)

SPEC_NOTIFICATION = ToolSpec(
    name="send_customer_notification",
    description=(
        "Send a customer a message from a fixed template. This CONTACTS THE "
        "CUSTOMER, cannot be unsent, and requires human approval. Refuses if the "
        "customer has opted out of contact."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "e.g. SYN_CUS_A0012"},
            "template": {
                "type": "string",
                # A fixed set, not free text. The model chooses WHICH message is
                # appropriate; it does not get to compose one, because composed
                # text reaching a customer is an injection sink.
                "enum": ["payment_failed_retry", "duplicate_refund_issued",
                         "payment_link_sent", "action_required"],
                "description": "Which approved message to send.",
            },
            "channel": {"type": "string", "enum": ["email", "sms"]},
            "reason": {"type": "string", "description": "Why contact is warranted."},
        },
        "required": ["customer_id", "template", "channel", "reason"],
    },
    required_permissions=["action:recover"],
    risk_class=RiskClass.MEDIUM,
    audit_required=True,
    idempotent=True,
    reversible=False,
)


def derive_action_key(merchant_id: str, target: str, action_type: str,
                      approval_id: str) -> str:
    """Server-held facts only — the same rule as refunds (§31).

    `approval_id` is included so a separately approved second contact is a
    genuinely distinct action, while any retry of the SAME approved action
    collapses onto one key and cannot send twice.
    """
    raw = f"{merchant_id}|{target}|{action_type}|{approval_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class ActionOutcome:
    action: AgentAction | None
    result: ToolResult


def _reserve(session, *, task_id, merchant_id, action_type, target, amount_minor,
             key, approval_id) -> tuple[AgentAction | None, ToolResult | None]:
    """Claim the action before calling out. UNIQUE(idempotency_key) is what makes
    a duplicate send impossible rather than merely unlikely."""
    action = AgentAction(
        id=f"ACT_{uuid.uuid4().hex[:12].upper()}", task_id=task_id,
        merchant_id=merchant_id, action_type=action_type, target_payment_id=target,
        amount_minor=amount_minor, idempotency_key=key,
        status=ActionStatus.PENDING, approval_id=approval_id,
    )
    # SAVEPOINT, not a bare flush: a collision must discard this INSERT, not the
    # approval decision and audit trail already written for this task.
    sp = session.begin_nested()
    try:
        session.add(action)
        session.flush()
        sp.commit()
        # Durable before we contact anyone. A notification cannot be unsent and
        # a payment link, once delivered, has been delivered — so the row that
        # says we did it must survive a failure on the way back.
        checkpoint(session)
        return action, None
    except IntegrityError:
        sp.rollback()
        prior = session.execute(text("""
            SELECT id, status, external_reference, verification_state
            FROM agent_actions WHERE idempotency_key = :k
        """), {"k": key}).mappings().first()
        return None, ToolResult(
            success=False, error_code="PARTIAL_EXECUTION",
            data={"error": "duplicate_action",
                  "detail": "This exact action was already attempted; not contacting "
                            "the customer again.",
                  "existing_action": dict(prior) if prior else None},
            risk_level="MEDIUM")


def execute_payment_link(session, adapter, *, task_id: str, merchant_id: str,
                         approval_id: str, synthetic_payment_id: str,
                         **_ignored) -> ActionOutcome:
    """Create a payment link for a failed payment. Amount comes from the row."""
    row = session.execute(text("""
        SELECT id, customer_id, amount_minor, status, external_payment_id
        FROM payments WHERE id = :p AND merchant_id = :m
    """), {"p": synthetic_payment_id, "m": merchant_id}).mappings().first()
    if row is None:
        return ActionOutcome(None, ToolResult(
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "unknown_payment"}, risk_level="MEDIUM"))
    # Re-validated at execution time (§29). A link for a payment that already
    # succeeded would ask a customer to pay twice.
    if row["status"] != "failed":
        return ActionOutcome(None, ToolResult(
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "payment_did_not_fail",
                  "detail": f"{synthetic_payment_id} is {row['status']}; a recovery "
                            f"link would ask the customer to pay again."},
            risk_level="MEDIUM"))

    amount = int(row["amount_minor"])
    key = derive_action_key(merchant_id, synthetic_payment_id, "payment_link", approval_id)
    action, dup = _reserve(session, task_id=task_id, merchant_id=merchant_id,
                           action_type="payment_link", target=synthetic_payment_id,
                           amount_minor=amount, key=key, approval_id=approval_id)
    if dup is not None:
        return ActionOutcome(None, dup)

    try:
        link = adapter.create_payment_link(
            merchant_id=merchant_id, customer_id=row["customer_id"],
            amount_minor=amount, source_payment_id=synthetic_payment_id,
            idempotency_key=key)
    except ProviderTimeout as e:
        action.status = ActionStatus.UNKNOWN
        action.verification_state = VerificationState.UNKNOWN
        action.verification_detail = {"reason": str(e)}
        action.verify_attempts += 1
        session.flush()
        return ActionOutcome(action, ToolResult(
            success=False, error_code="EXTERNAL_STATE_UNKNOWN",
            data={"action_id": action.id, "error": str(e)}, risk_level="MEDIUM"))
    except ProviderError as e:
        action.status = ActionStatus.FAILED
        action.verification_state = VerificationState.FAILED
        action.verification_detail = {"reason": str(e)}
        session.flush()
        return ActionOutcome(action, ToolResult(
            success=False, error_code=e.code, data={"error": str(e)}, risk_level="MEDIUM"))

    action.status = ActionStatus.SUBMITTED
    action.external_reference = link.id
    session.flush()

    vr = verify_payment_link(adapter, link_id=link.id, expected_amount_minor=amount)
    action.verification_state = vr.state
    action.verification_detail = vr.as_dict()
    action.verify_attempts += 1
    action.status = (ActionStatus.CONFIRMED if vr.state is VerificationState.SUCCESS
                     else ActionStatus.UNKNOWN if vr.state is VerificationState.UNKNOWN
                     else ActionStatus.FAILED)
    session.flush()

    return ActionOutcome(action, ToolResult(
        success=vr.state is VerificationState.SUCCESS,
        data={"action_id": action.id, "payment_link_id": link.id,
              "short_url": link.short_url, "amount_minor": amount,
              "verification": vr.as_dict()},
        evidence=[
            Evidence(key="payment_link_id", value=link.id, source="razorpay"),
            Evidence(key="verification_state", value=vr.state.value, source="verification"),
        ],
        external_reference=link.id, risk_level="MEDIUM", approval_id=approval_id))


def execute_notification(session, adapter, *, task_id: str, merchant_id: str,
                         approval_id: str, customer_id: str, template: str,
                         channel: str, **_ignored) -> ActionOutcome:
    row = session.execute(text("""
        SELECT id, contact_opted_out FROM customers WHERE id = :c AND merchant_id = :m
    """), {"c": customer_id, "m": merchant_id}).mappings().first()
    if row is None:
        return ActionOutcome(None, ToolResult(
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "unknown_customer"}, risk_level="MEDIUM"))
    # Re-checked here and not only in the planner. §28's opt-out is a property
    # of the customer, and every path that could contact them has to honour it —
    # including a human approving a stale recommendation.
    if row["contact_opted_out"]:
        return ActionOutcome(None, ToolResult(
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "customer_opted_out",
                  "detail": f"{customer_id} has opted out of contact."},
            risk_level="MEDIUM"))

    key = derive_action_key(merchant_id, f"{customer_id}:{template}",
                            "notification", approval_id)
    action, dup = _reserve(session, task_id=task_id, merchant_id=merchant_id,
                           action_type="notification", target=customer_id,
                           amount_minor=0, key=key, approval_id=approval_id)
    if dup is not None:
        return ActionOutcome(None, dup)

    try:
        notif = adapter.create_notification(
            merchant_id=merchant_id, customer_id=customer_id, channel=channel,
            template=template, idempotency_key=key)
    except ProviderTimeout as e:
        # A message may or may not have gone out. UNKNOWN, and never a blind
        # retry: the failure mode is contacting someone twice.
        action.status = ActionStatus.UNKNOWN
        action.verification_state = VerificationState.UNKNOWN
        action.verification_detail = {"reason": str(e)}
        action.verify_attempts += 1
        session.flush()
        return ActionOutcome(action, ToolResult(
            success=False, error_code="EXTERNAL_STATE_UNKNOWN",
            data={"action_id": action.id, "error": str(e)}, risk_level="MEDIUM"))
    except ProviderError as e:
        action.status = ActionStatus.FAILED
        action.verification_state = VerificationState.FAILED
        action.verification_detail = {"reason": str(e)}
        session.flush()
        return ActionOutcome(action, ToolResult(
            success=False, error_code=e.code, data={"error": str(e)}, risk_level="MEDIUM"))

    action.status = ActionStatus.SUBMITTED
    action.external_reference = notif.id
    session.flush()

    vr = verify_notification(adapter, notification_id=notif.id)
    action.verification_state = vr.state
    action.verification_detail = vr.as_dict()
    action.verify_attempts += 1
    action.status = (ActionStatus.CONFIRMED if vr.state is VerificationState.SUCCESS
                     else ActionStatus.UNKNOWN if vr.state is VerificationState.UNKNOWN
                     else ActionStatus.FAILED)
    session.flush()

    return ActionOutcome(action, ToolResult(
        success=vr.state is VerificationState.SUCCESS,
        data={"action_id": action.id, "notification_id": notif.id,
              "channel": channel, "template": template, "verification": vr.as_dict()},
        evidence=[
            Evidence(key="notification_id", value=notif.id, source="razorpay"),
            Evidence(key="verification_state", value=vr.state.value, source="verification"),
        ],
        external_reference=notif.id, risk_level="MEDIUM", approval_id=approval_id))


# --------------------------------------------------------------------------
# Verification predicates (§32) — read the object back, never trust the response
# --------------------------------------------------------------------------
def verify_payment_link(adapter, *, link_id: str,
                        expected_amount_minor: int) -> VerificationResult:
    expected = {"payment_link_id": link_id, "amount_minor": expected_amount_minor}
    try:
        link = adapter.get_payment_link(link_id)
    except Exception as e:                                          # noqa: BLE001
        return VerificationResult(VerificationState.UNKNOWN,
                                  f"Could not read the payment link back: {e}.",
                                  expected, {}, link_id)
    if link is None:
        return VerificationResult(VerificationState.FAILED,
                                  "The provider has no such payment link.",
                                  expected, {}, link_id)
    actual = {"status": link.status, "amount_minor": link.amount_minor,
              "short_url": link.short_url}
    if link.amount_minor != expected_amount_minor:
        return VerificationResult(
            VerificationState.PARTIAL,
            f"The link exists but is for {link.amount_minor / 100:,.2f}, not "
            f"{expected_amount_minor / 100:,.2f}.", expected, actual, link_id)
    if link.status in ("created", "paid"):
        return VerificationResult(VerificationState.SUCCESS,
                                  f"Payment link {link_id} exists and is {link.status}.",
                                  expected, actual, link_id)
    return VerificationResult(
        VerificationState.FAILED,
        f"Payment link {link_id} is {link.status}; the customer cannot use it.",
        expected, actual, link_id)


def verify_notification(adapter, *, notification_id: str) -> VerificationResult:
    expected = {"notification_id": notification_id, "status": "sent"}
    try:
        n = adapter.get_notification(notification_id)
    except Exception as e:                                          # noqa: BLE001
        return VerificationResult(VerificationState.UNKNOWN,
                                  f"Could not read the notification back: {e}.",
                                  expected, {}, notification_id)
    if n is None:
        # Reported as UNKNOWN rather than FAILED. A provider with no read-back
        # is a provider we cannot question, and claiming the message did not go
        # out would be exactly as unfounded as claiming it did (§33).
        return VerificationResult(
            VerificationState.UNKNOWN,
            "The provider returned no record for this notification, so whether "
            "the customer was contacted cannot be established.",
            expected, {}, notification_id)
    actual = {"status": n.status, "channel": n.channel}
    if n.status == "sent":
        return VerificationResult(VerificationState.SUCCESS,
                                  f"Notification {notification_id} was sent.",
                                  expected, actual, notification_id)
    if n.status == "queued":
        return VerificationResult(VerificationState.UNKNOWN,
                                  f"Notification {notification_id} is still queued.",
                                  expected, actual, notification_id)
    return VerificationResult(VerificationState.FAILED,
                              f"Notification {notification_id} is {n.status}.",
                              expected, actual, notification_id)
