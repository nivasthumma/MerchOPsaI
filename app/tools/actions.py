"""Action tools — CONTRACT §13, §23, §24 (as amended by ADR-0008 #1, #2).

The refund path in full:

    approval verified (server-side)
        -> resolve synthetic -> external id via the mapping layer (§6)
        -> derive idempotency key SERVER-SIDE (§13; never model-supplied)
        -> reserve agent_actions row (UNIQUE key claims the action) (§24)
        -> external call
        -> record external reference
        -> independent verification (§25)
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.integrations.razorpay.adapter import RazorpayAdapter
from app.integrations.razorpay.faults import ProviderError, ProviderTimeout
from app.models import ActionStatus, AgentAction, VerificationState
from app.tools.contracts import Evidence, RiskClass, ToolResult, ToolSpec
from app.verification.engine import VerificationResult, verify_refund

SPEC_REQUEST_REFUND = ToolSpec(
    name="request_refund",
    description=(
        "Request a refund for a captured payment. This is a HIGH-risk financial "
        "action: it always requires human approval and can never execute directly "
        "from this call. Provide the synthetic payment id (e.g. SYN_PAY_0002) and "
        "the amount in minor units (paise)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "synthetic_payment_id": {
                "type": "string",
                "description": "Internal payment id, e.g. SYN_PAY_0002. Never a provider id.",
            },
            "amount_minor": {
                "type": "integer", "minimum": 1,
                "description": "Refund amount in paise.",
            },
            "reason": {"type": "string", "description": "Why the refund is warranted."},
        },
        # NOTE: idempotency_key is deliberately ABSENT. CONTRACT §13 (amended):
        # a model-supplied key defeats deduplication, because on retry the model
        # emits a fresh key and the uniqueness check misses.
        "required": ["synthetic_payment_id", "amount_minor", "reason"],
    },
    required_permissions=["action:refund"],
    risk_class=RiskClass.HIGH,
    audit_required=True,
    idempotent=True,
)

SPEC_REFUND_STATUS = ToolSpec(
    name="get_refund_status",
    description="Read the current state of a refund action previously created for this task.",
    input_schema={
        "type": "object",
        "properties": {"action_id": {"type": "string"}},
        "required": ["action_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def derive_idempotency_key(merchant_id: str, external_payment_id: str,
                           action_type: str, approval_id: str) -> str:
    """CONTRACT §13 (amended). Derived from server-held facts only.

    approval_id is included so that a *separately approved* second refund is a
    genuinely distinct action, while any retry of the SAME approved action
    collapses onto one key.
    """
    raw = f"{merchant_id}|{external_payment_id}|{action_type}|{approval_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class RefundOutcome:
    action: AgentAction
    result: ToolResult


def resolve_external_payment(session, merchant_id: str, synthetic_payment_id: str) -> tuple[str | None, dict]:
    """CONTRACT §6 mapping layer. The agent must never invent a provider id;
    the only path from synthetic to external runs through here."""
    row = session.execute(text("""
        SELECT id, merchant_id, external_provider, external_payment_id,
               amount_minor, amount_refunded_minor, status
        FROM payments WHERE id = :p
    """), {"p": synthetic_payment_id}).mappings().first()
    if row is None:
        return None, {"error": "unknown_payment"}
    if row["merchant_id"] != merchant_id:
        return None, {"error": "merchant_isolation"}
    if not row["external_payment_id"]:
        return None, {"error": "not_externally_mapped",
                      "detail": (f"{synthetic_payment_id} has no external provider mapping. "
                                 "Only the mapped subset can be executed externally.")}
    return row["external_payment_id"], {
        "amount_minor": int(row["amount_minor"]),
        "amount_refunded_minor": int(row["amount_refunded_minor"]),
        "status": row["status"],
    }


def execute_refund(
    session,
    adapter: RazorpayAdapter,
    *,
    task_id: str,
    merchant_id: str,
    synthetic_payment_id: str,
    amount_minor: int,
    approval_id: str,
) -> RefundOutcome:
    """Executes an APPROVED refund. Callers must have verified the approval."""
    external_id, meta = resolve_external_payment(session, merchant_id, synthetic_payment_id)
    if external_id is None:
        return RefundOutcome(
            action=None,  # type: ignore[arg-type]
            result=ToolResult(success=False, error_code="TOOL_INVALID_ARGUMENT",
                              data=meta, risk_level="HIGH"),
        )

    # --- CONTRACT §23: re-validate preconditions at execution time --------
    if meta["status"] == "failed":
        return RefundOutcome(None, ToolResult(  # type: ignore[arg-type]
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "payment_not_captured"}, risk_level="HIGH"))
    remaining = meta["amount_minor"] - meta["amount_refunded_minor"]
    if amount_minor > remaining:
        return RefundOutcome(None, ToolResult(  # type: ignore[arg-type]
            success=False, error_code="TOOL_INVALID_ARGUMENT",
            data={"error": "exceeds_refundable_balance", "remaining_minor": remaining},
            risk_level="HIGH"))

    key = derive_idempotency_key(merchant_id, external_id, "refund", approval_id)

    # --- CONTRACT §24: RESERVE before calling -----------------------------
    action = AgentAction(
        id=f"ACT_{uuid.uuid4().hex[:12].upper()}", task_id=task_id, merchant_id=merchant_id,
        action_type="refund", target_payment_id=synthetic_payment_id,
        external_payment_id=external_id, amount_minor=amount_minor,
        idempotency_key=key, status=ActionStatus.PENDING, approval_id=approval_id,
    )
    session.add(action)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        prior = session.execute(text("""
            SELECT id, status, external_reference, verification_state
            FROM agent_actions WHERE idempotency_key = :k
        """), {"k": key}).mappings().first()
        return RefundOutcome(None, ToolResult(  # type: ignore[arg-type]
            success=False, error_code="PARTIAL_EXECUTION",
            data={"error": "duplicate_action",
                  "detail": "This exact action was already attempted; not calling the provider again.",
                  "existing_action": dict(prior) if prior else None},
            risk_level="HIGH"))

    refunded_before = meta["amount_refunded_minor"]

    # --- external call ----------------------------------------------------
    external_ref: str | None = None
    try:
        ext = adapter.create_refund(external_id, amount_minor, key)
        external_ref = ext.id
        action.status = ActionStatus.SUBMITTED
        action.external_reference = external_ref
        session.flush()
    except ProviderTimeout as e:
        # The outcome is genuinely unknown. Do NOT retry blindly (CONTRACT §35).
        action.status = ActionStatus.UNKNOWN
        session.flush()

        # Reconcile immediately: ask the provider about our own key. If the
        # outage has cleared we recover the reference and can settle the state
        # honestly instead of parking it in UNKNOWN.
        recovered = None
        try:
            recovered = adapter.find_refund_by_idempotency_key(key)
        except Exception:
            recovered = None
        if recovered is not None:
            action.external_reference = recovered.id
            session.flush()

        vr = verify_refund(adapter, external_payment_id=external_id,
                           expected_refund_minor=amount_minor,
                           refunded_before_minor=refunded_before,
                           external_reference=action.external_reference)
        if e.submitted and vr.state is VerificationState.SUCCESS \
                and action.external_reference is None:
            # The payment shows a refund, but with no reference we cannot
            # attribute it to THIS action. Claiming SUCCESS here would be
            # exactly the false verification CONTRACT §25 forbids.
            vr = type(vr)(VerificationState.UNKNOWN,
                          f"{e}. The payment shows a refund, but no external reference "
                          f"was received, so this action cannot be confirmed as its cause.",
                          vr.expected, vr.actual, None)
        elif e.submitted and vr.state is VerificationState.FAILED:
            # We could read state and it is unchanged, but the request may have
            # been in flight. Downgrade to UNKNOWN rather than assert failure.
            vr = type(vr)(VerificationState.UNKNOWN,
                          f"{e}. State currently shows no refund, but the request may "
                          f"still be in flight. Refusing to report FAILED.",
                          vr.expected, vr.actual, None)
        action.verification_state = vr.state
        action.verification_detail = vr.as_dict()
        action.verify_attempts += 1
        session.flush()
        return RefundOutcome(action, ToolResult(
            success=False, error_code="EXTERNAL_STATE_UNKNOWN",
            data={"verification": vr.as_dict(), "action_id": action.id},
            evidence=[Evidence(key="verification_state", value=vr.state.value, source="verification")],
            external_reference=None, risk_level="HIGH", approval_id=approval_id))
    except ProviderError as e:
        action.status = ActionStatus.FAILED
        action.verification_state = VerificationState.FAILED
        action.verification_detail = {"reason": str(e)}
        session.flush()
        return RefundOutcome(action, ToolResult(
            success=False, error_code=e.code, data={"error": str(e), "action_id": action.id},
            risk_level="HIGH", approval_id=approval_id))

    # --- CONTRACT §25: independent verification ---------------------------
    vr = verify_refund(adapter, external_payment_id=external_id,
                       expected_refund_minor=amount_minor,
                       refunded_before_minor=refunded_before,
                       external_reference=external_ref)
    action.verification_state = vr.state
    action.verification_detail = vr.as_dict()
    action.verify_attempts += 1
    action.status = {
        VerificationState.SUCCESS: ActionStatus.CONFIRMED,
        VerificationState.FAILED: ActionStatus.FAILED,
        VerificationState.PARTIAL: ActionStatus.SUBMITTED,
        VerificationState.UNKNOWN: ActionStatus.UNKNOWN,
    }[vr.state]
    session.flush()

    return RefundOutcome(action, ToolResult(
        success=vr.state is VerificationState.SUCCESS,
        data={"verification": vr.as_dict(), "action_id": action.id,
              "amount_minor": amount_minor, "payment_id": synthetic_payment_id},
        evidence=[
            Evidence(key="external_reference", value=external_ref, source="razorpay"),
            Evidence(key="verification_state", value=vr.state.value, source="verification"),
            Evidence(key="amount_refunded_after",
                     value=vr.actual.get("amount_refunded_minor"), source="razorpay"),
        ],
        external_reference=external_ref, risk_level="HIGH", approval_id=approval_id))


def reverify_action(session, adapter: RazorpayAdapter, action: AgentAction) -> VerificationResult:
    """CONTRACT §26 (amended) — the UNKNOWN exit path."""
    before = action.verification_detail or {}
    refunded_before = int(before.get("expected", {}).get("amount_refunded_before_minor", 0))

    # Reconcile by our own idempotency key first. After a timeout we hold no
    # external reference, so without this lookup the system can never learn
    # whether the action landed and UNKNOWN would be a dead end.
    reference = action.external_reference
    if reference is None:
        try:
            found = adapter.find_refund_by_idempotency_key(action.idempotency_key)
        except Exception:
            found = None
        if found is not None:
            reference = found.id
            action.external_reference = reference

    vr = verify_refund(adapter, external_payment_id=action.external_payment_id or "",
                       expected_refund_minor=action.amount_minor,
                       refunded_before_minor=refunded_before,
                       external_reference=reference)
    action.verification_state = vr.state
    action.verification_detail = vr.as_dict()
    action.verify_attempts += 1
    action.status = {
        VerificationState.SUCCESS: ActionStatus.CONFIRMED,
        VerificationState.FAILED: ActionStatus.FAILED,
        VerificationState.PARTIAL: ActionStatus.SUBMITTED,
        VerificationState.UNKNOWN: ActionStatus.UNKNOWN,
    }[vr.state]
    session.flush()
    return vr
