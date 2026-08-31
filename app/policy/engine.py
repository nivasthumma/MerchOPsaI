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
from app.policy.risk import RiskAssessment, assess
from app.tools.registry import REGISTRY


# MerchantOps §41. Bumped by hand, because a policy version is a statement
# about the RULES rather than about the code that expresses them: a refactor
# that leaves every decision identical is not a new policy, and a threshold
# change with no diff elsewhere is.
POLICY_VERSION = "policy-v3"


class Decision(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    # MerchantOps §25. A second pair of eyes, from a different person -- the
    # "different" part is a UNIQUE constraint on approval_signatures, not a
    # check this module performs.
    REQUIRE_DUAL_APPROVAL = "REQUIRE_DUAL_APPROVAL"


@dataclass
class PolicyContext:
    """Everything the engine is allowed to consider. Note what is absent:
    no model output, no free text, no client-supplied role."""
    tenant_id: str
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
    risk: RiskAssessment | None = None

    @property
    def required_signatures(self) -> int:
        """How many distinct humans must sign before this may execute."""
        return 2 if self.decision is Decision.REQUIRE_DUAL_APPROVAL else 1

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value, "reason": self.reason, "rule": self.rule,
            "risk_level": self.risk_level, "approval_required": self.approval_required,
            "required_signatures": self.required_signatures,
            "risk": self.risk.as_dict() if self.risk else None,
            "details": self.details,
        }


# MerchantOps §24 — a tool's risk and its required permissions are declared ONCE,
# in the registry, and read from there.
#
# They used to be declared twice: on the ToolSpec and again in dicts here, with
# the engine's copy silently winning. The two agreed, so nothing was broken --
# but a tool added to the registry and forgotten here was denied as an
# unregistered tool, and a permission list that drifted would have been a
# security control disagreeing with its own documentation. Nine more tools are
# due in §18; the duplication is retired before they arrive rather than after.
#
# The declared class is a FLOOR. app.policy.risk may raise a call above it.


def declared_risk(tool_name: str) -> str | None:
    spec = REGISTRY.get(tool_name)
    return spec.risk_class.value if spec else None


def required_permissions(tool_name: str) -> list[str]:
    spec = REGISTRY.get(tool_name)
    return list(spec.required_permissions) if spec else []


def evaluate(session, ctx: PolicyContext) -> PolicyResult:
    """CONTRACT §20 decision flow, in order. First failing gate wins."""
    s = get_settings()

    # ---- 1. Tool must be registered -------------------------------------
    spec = REGISTRY.get(ctx.tool_name)
    if spec is None:
        return PolicyResult(Decision.DENY,
                            f"Tool '{ctx.tool_name}' is not in the registry.",
                            "unregistered_tool", ctx.risk_level)

    # The declared class only. Computed risk is graded after the cheap gates:
    # it reads the database, and a caller with no permission should be refused
    # before we spend a query grading how dangerous their refused call was.
    risk = spec.risk_class.value

    # ---- 2. Permission ---------------------------------------------------
    required = required_permissions(ctx.tool_name)
    missing = [p for p in required if p not in ctx.permissions]
    if missing:
        return PolicyResult(
            Decision.DENY,
            f"User {ctx.user_id} (role={ctx.role}) lacks required permission(s): {', '.join(missing)}.",
            "missing_permission", risk, details={"missing": missing, "required": required},
        )

    # ---- 3. Resource ownership: tenant, then merchant (§38, §54) --------
    # Two boundaries, checked outermost first. Merchant isolation does the work
    # on every request; tenant isolation is the one that still holds if merchant
    # isolation is ever wrong, and it produces a different rule name so the two
    # are distinguishable in an audit trail rather than both reading
    # "isolation".
    target_payment = ctx.arguments.get("synthetic_payment_id") or ctx.arguments.get("payment_id")
    if target_payment:
        owner = session.execute(text("""
            SELECT p.merchant_id, m.tenant_id FROM payments p
            JOIN merchants m ON m.id = p.merchant_id WHERE p.id = :p
        """), {"p": target_payment}).mappings().first()
        if owner is None:
            return PolicyResult(Decision.DENY,
                                f"Payment {target_payment} does not exist.",
                                "unknown_resource", risk)
        if owner["tenant_id"] != ctx.tenant_id:
            return PolicyResult(
                Decision.DENY,
                f"Payment {target_payment} belongs to another tenant. Cross-tenant "
                f"access denied.",
                "tenant_isolation", risk,
                details={"requested_by_tenant": ctx.tenant_id},
            )
        if owner["merchant_id"] != ctx.merchant_id:
            return PolicyResult(
                Decision.DENY,
                f"Payment {target_payment} belongs to another merchant. Cross-merchant access denied.",
                "merchant_isolation", risk,
                details={"requested_by_merchant": ctx.merchant_id},
            )

    order_id = ctx.arguments.get("order_id")
    if order_id:
        owner = session.execute(text("""
            SELECT o.merchant_id, m.tenant_id FROM orders o
            JOIN merchants m ON m.id = o.merchant_id WHERE o.id = :o
        """), {"o": order_id}).mappings().first()
        if owner is not None and owner["tenant_id"] != ctx.tenant_id:
            return PolicyResult(
                Decision.DENY,
                f"Order {order_id} belongs to another tenant. Cross-tenant access denied.",
                "tenant_isolation", risk,
                details={"requested_by_tenant": ctx.tenant_id},
            )
        if owner is not None and owner["merchant_id"] != ctx.merchant_id:
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

    # ---- 5. Financial constraints ---------------------------------------
    # A positive integer amount is required of ANY tool that names one.
    amount = ctx.arguments.get("amount_minor")
    if amount is not None and (not isinstance(amount, int) or amount <= 0):
        return PolicyResult(Decision.DENY, f"Invalid amount: {amount!r}.",
                            "invalid_amount", risk)

    # The rest are REFUND constraints and are scoped to refunds. They were not,
    # and the moment a second money-shaped tool existed that mattered: a payment
    # link carrying an amount was measured against the merchant's *refund* limit
    # and refused with a message about refunds. The denial happened to be
    # convenient; the reasoning was wrong, and a limit that fires for the wrong
    # reason is a limit nobody can predict. Each financial tool gets its own
    # bounds when it has any.
    if ctx.tool_name == "request_refund" and amount is not None:
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

    # ---- 7. Grade the call, then gate on the graded risk -----------------
    # MerchantOps §24. Everything above this point either denied the call or
    # established that it is permitted in principle; what remains is how
    # closely a human has to look. That is the one question the declared class
    # alone cannot answer, because it does not know the amount.
    assessment = assess(session, tool_name=ctx.tool_name, declared=risk,
                        merchant_id=ctx.merchant_id, arguments=ctx.arguments,
                        spec=spec)

    if assessment.level == "CRITICAL":
        return PolicyResult(
            Decision.REQUIRE_DUAL_APPROVAL,
            "Critical-risk financial action requires two separate approvers. "
            + "; ".join(f.reason for f in assessment.factors if f.level == "CRITICAL"),
            "critical_risk_requires_dual_approval", assessment.level,
            approval_required=True, risk=assessment,
            details={"risk": assessment.as_dict()},
        )

    return PolicyResult(
        Decision.REQUIRE_APPROVAL,
        "Financial state-changing action requires human approval.",
        "high_risk_requires_approval", assessment.level,
        approval_required=True, risk=assessment,
        details={"risk": assessment.as_dict()},
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
