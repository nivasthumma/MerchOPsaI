"""Deterministic policy engine — CONTRACT §19, §20.

This module is the authorization authority. The LLM never reaches it with
anything but a *request*; it cannot influence the outcome. Nothing here reads
model output: every input is either the authenticated session or a database
fact.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import get_settings


class Decision(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


@dataclass
class PolicyContext:
    """Everything the engine is allowed to consider. Note what is absent:
    no model output, no free text, no client-supplied role."""
    user_id: str
    merchant_id: str
    role: str
    permissions: list[str]
    tool_name: str
    risk_level: str
    arguments: dict = field(default_factory=dict)


@dataclass
class PolicyResult:
    decision: Decision
    reason: str
    rule: str
    risk_level: str
    approval_required: bool = False
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value, "reason": self.reason, "rule": self.rule,
            "risk_level": self.risk_level, "approval_required": self.approval_required,
            "details": self.details,
        }


# CONTRACT §19 — risk is a property of the TOOL, declared in the registry,
# never inferred from the model's description of what it wants to do.
TOOL_RISK: dict[str, str] = {
    "get_revenue_summary": "LOW",
    "get_payment_metrics": "LOW",
    "find_duplicate_payments": "LOW",
    "get_order": "LOW",
    "request_refund": "HIGH",
    "get_refund_status": "LOW",
}

TOOL_PERMISSIONS: dict[str, list[str]] = {
    "get_revenue_summary": ["read:metrics"],
    "get_payment_metrics": ["read:metrics"],
    "find_duplicate_payments": ["read:orders"],
    "get_order": ["read:orders"],
    "request_refund": ["action:refund"],
    "get_refund_status": ["read:orders"],
}


def evaluate(session, ctx: PolicyContext) -> PolicyResult:
    """CONTRACT §20 decision flow, in order. First failing gate wins."""
    s = get_settings()

    # ---- 1. Tool must be registered -------------------------------------
    if ctx.tool_name not in TOOL_RISK:
        return PolicyResult(Decision.DENY,
                            f"Tool '{ctx.tool_name}' is not in the registry.",
                            "unregistered_tool", ctx.risk_level)

    risk = TOOL_RISK[ctx.tool_name]

    # ---- 2. Permission ---------------------------------------------------
    required = TOOL_PERMISSIONS.get(ctx.tool_name, [])
    missing = [p for p in required if p not in ctx.permissions]
    if missing:
        return PolicyResult(
            Decision.DENY,
            f"User {ctx.user_id} (role={ctx.role}) lacks required permission(s): {', '.join(missing)}.",
            "missing_permission", risk, details={"missing": missing, "required": required},
        )

    # ---- 3. Resource ownership / merchant scope (CONTRACT §38) ----------
    target_payment = ctx.arguments.get("synthetic_payment_id") or ctx.arguments.get("payment_id")
    if target_payment:
        owner = session.execute(
            text("SELECT merchant_id FROM payments WHERE id = :p"), {"p": target_payment}
        ).scalar()
        if owner is None:
            return PolicyResult(Decision.DENY,
                                f"Payment {target_payment} does not exist.",
                                "unknown_resource", risk)
        if owner != ctx.merchant_id:
            return PolicyResult(
                Decision.DENY,
                f"Payment {target_payment} belongs to another merchant. Cross-merchant access denied.",
                "merchant_isolation", risk,
                details={"requested_by_merchant": ctx.merchant_id},
            )

    order_id = ctx.arguments.get("order_id")
    if order_id:
        owner = session.execute(
            text("SELECT merchant_id FROM orders WHERE id = :o"), {"o": order_id}
        ).scalar()
        if owner is not None and owner != ctx.merchant_id:
            return PolicyResult(
                Decision.DENY,
                f"Order {order_id} belongs to another merchant. Cross-merchant access denied.",
                "merchant_isolation", risk,
                details={"requested_by_merchant": ctx.merchant_id},
            )

    # ---- 4. LOW risk is allowed once authorized --------------------------
    if risk == "LOW":
        return PolicyResult(Decision.ALLOW, "Read-only operation, user authorized.",
                            "low_risk_authorized", risk)

    # ---- 5. Financial constraints (HIGH risk only) -----------------------
    amount = ctx.arguments.get("amount_minor")
    if amount is not None:
        if not isinstance(amount, int) or amount <= 0:
            return PolicyResult(Decision.DENY, f"Invalid refund amount: {amount!r}.",
                                "invalid_amount", risk)

        merchant_limit = session.execute(
            text("SELECT policy_config FROM merchants WHERE id = :m"), {"m": ctx.merchant_id}
        ).scalar() or {}
        limit = int(merchant_limit.get("refund_limit_minor", s.refund_amount_limit_minor))
        if amount > limit:
            return PolicyResult(
                Decision.DENY,
                f"Refund amount {amount/100:,.2f} exceeds the merchant limit of {limit/100:,.2f}.",
                "amount_limit_exceeded", risk,
                details={"amount_minor": amount, "limit_minor": limit},
            )

        if target_payment:
            row = session.execute(text("""
                SELECT amount_minor, amount_refunded_minor, status
                FROM payments WHERE id = :p
            """), {"p": target_payment}).mappings().first()
            if row:
                if row["status"] == "failed":
                    return PolicyResult(Decision.DENY,
                                        f"Payment {target_payment} was never captured; it cannot be refunded.",
                                        "payment_not_refundable", risk)
                remaining = int(row["amount_minor"]) - int(row["amount_refunded_minor"])
                if amount > remaining:
                    return PolicyResult(
                        Decision.DENY,
                        f"Refund of {amount/100:,.2f} exceeds the refundable balance "
                        f"of {remaining/100:,.2f} on {target_payment}.",
                        "exceeds_refundable_balance", risk,
                        details={"remaining_minor": remaining},
                    )

    # ---- 6. Duplicate-action check (CONTRACT §20, §24) -------------------
    if ctx.tool_name == "request_refund" and target_payment:
        existing = session.execute(text("""
            SELECT id, status FROM agent_actions
            WHERE merchant_id = :m AND target_payment_id = :p
              AND action_type = 'refund' AND status IN ('PENDING','SUBMITTED','CONFIRMED')
        """), {"m": ctx.merchant_id, "p": target_payment}).mappings().first()
        if existing:
            return PolicyResult(
                Decision.DENY,
                f"A refund action for {target_payment} already exists ({existing['id']}, "
                f"status={existing['status']}). Refusing to create a duplicate financial action.",
                "duplicate_action", risk, details={"existing_action_id": existing["id"]},
            )

    # ---- 7. HIGH risk always requires human approval (CONTRACT §19) ------
    return PolicyResult(
        Decision.REQUIRE_APPROVAL,
        "Financial state-changing action requires human approval.",
        "high_risk_requires_approval", risk, approval_required=True,
    )


def approval_is_valid(approval, now: datetime | None = None) -> tuple[bool, str]:
    """CONTRACT §21 — approvals expire. Checked server-side at execution time."""
    now = now or datetime.now(timezone.utc)
    if approval is None:
        return False, "No approval record exists for this action."
    if approval.decision == "REJECTED":
        return False, "The action was rejected by a human reviewer."
    if approval.decision != "APPROVED":
        return False, f"Approval is not granted (state={approval.decision})."
    exp = approval.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        return False, f"Approval {approval.id} expired at {exp.isoformat()}."
    return True, "Approval valid."
