"""Typed tool registry — CONTRACT §12, §13.

The registry is the *only* set of operations reachable by the model. There is
no dynamic dispatch, no eval, no URL construction, no SQL from model text.
"""
from __future__ import annotations

from app.tools.actions import (
    SPEC_REFUND_STATUS, SPEC_REQUEST_REFUND, get_refund_status,
)
from app.tools.contracts import ToolResult, ToolSpec
from app.tools.investigation import (
    SPEC_DUPLICATES, SPEC_FAILURE_BREAKDOWN, SPEC_GET_CUSTOMER, SPEC_GET_ORDER,
    SPEC_GET_PAYMENT, SPEC_PAYMENT_METRICS, SPEC_REVENUE,
    find_duplicate_payments, get_customer, get_failure_breakdown, get_order,
    get_payment, get_payment_metrics, get_revenue_summary,
)
from app.tools.recovery_actions import SPEC_NOTIFICATION, SPEC_PAYMENT_LINK
from app.tools.recovery_tools import (
    SPEC_RECOVERY_CANDIDATES, calculate_recovery_candidates,
)
from app.tools.verification_tools import (
    SPEC_PROVIDER_EVENT, SPEC_PAYMENT_STATUS, SPEC_RECONCILE,
    get_payment_status, get_provider_event, reconcile_transaction,
)

# MerchantOps §18's fifteen tools. The registry is the ONLY set of operations
# reachable by the model, and the single declaration of each tool's risk class
# and required permissions (ADR-0019).
REGISTRY: dict[str, ToolSpec] = {
    # investigation
    SPEC_REVENUE.name: SPEC_REVENUE,
    SPEC_PAYMENT_METRICS.name: SPEC_PAYMENT_METRICS,
    SPEC_FAILURE_BREAKDOWN.name: SPEC_FAILURE_BREAKDOWN,
    SPEC_DUPLICATES.name: SPEC_DUPLICATES,
    SPEC_GET_PAYMENT.name: SPEC_GET_PAYMENT,
    SPEC_GET_ORDER.name: SPEC_GET_ORDER,
    SPEC_GET_CUSTOMER.name: SPEC_GET_CUSTOMER,
    # recovery
    SPEC_RECOVERY_CANDIDATES.name: SPEC_RECOVERY_CANDIDATES,
    SPEC_REQUEST_REFUND.name: SPEC_REQUEST_REFUND,
    SPEC_PAYMENT_LINK.name: SPEC_PAYMENT_LINK,
    SPEC_NOTIFICATION.name: SPEC_NOTIFICATION,
    # verification
    SPEC_REFUND_STATUS.name: SPEC_REFUND_STATUS,
    SPEC_PAYMENT_STATUS.name: SPEC_PAYMENT_STATUS,
    SPEC_PROVIDER_EVENT.name: SPEC_PROVIDER_EVENT,
    SPEC_RECONCILE.name: SPEC_RECONCILE,
}

# Tools executed directly on request. Everything NOT listed here either has no
# implementation or is a state-changing action that must reach the provider
# through the approval path in app/agent/approval.py — never from here.
_READ_IMPL = {
    "get_revenue_summary": get_revenue_summary,
    "get_payment_metrics": get_payment_metrics,
    "get_failure_breakdown": get_failure_breakdown,
    "find_duplicate_payments": find_duplicate_payments,
    "get_payment": get_payment,
    "get_order": get_order,
    "get_customer": get_customer,
    "calculate_recovery_candidates": calculate_recovery_candidates,
    "get_refund_status": get_refund_status,
    "get_payment_status": get_payment_status,
    "get_provider_event": get_provider_event,
    "reconcile_transaction": reconcile_transaction,
}

# These need to reach the provider to answer, so they are handed an adapter.
_NEEDS_ADAPTER = frozenset({"get_payment_status", "reconcile_transaction"})


def validate_arguments(spec: ToolSpec, args: dict) -> tuple[bool, str | None]:
    """CONTRACT §13 — reject before execution, never at the provider."""
    schema = spec.input_schema
    props = schema.get("properties", {})
    unknown = [k for k in args if k not in props]
    if unknown:
        return False, f"Unknown argument(s): {', '.join(unknown)}"
    for name in schema.get("required", []):
        if name not in args:
            return False, f"Missing required argument: {name}"
    for k, v in args.items():
        p = props.get(k, {})
        types = p.get("type")
        types = [types] if isinstance(types, str) else (types or [])
        if not types:
            continue
        ok = False
        for t in types:
            if t == "string" and isinstance(v, str):
                ok = True
            elif t == "integer" and isinstance(v, int) and not isinstance(v, bool):
                ok = True
            elif t == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
                ok = True
            elif t == "boolean" and isinstance(v, bool):
                ok = True
            elif t == "null" and v is None:
                ok = True
            elif t in ("object", "array") and isinstance(v, (dict, list)):
                ok = True
        if not ok:
            return False, f"Argument '{k}' has invalid type: expected {types}, got {type(v).__name__}"
        if "minimum" in p and isinstance(v, (int, float)) and v < p["minimum"]:
            return False, f"Argument '{k}' below minimum {p['minimum']}"
        if "maximum" in p and isinstance(v, (int, float)) and v > p["maximum"]:
            return False, f"Argument '{k}' above maximum {p['maximum']}"
        if "enum" in p and v not in p["enum"]:
            return False, f"Argument '{k}' not one of {p['enum']}"
    return True, None


def execute_read_tool(session, name: str, merchant_id: str, args: dict,
                      frozen: dict | None = None, adapter=None) -> ToolResult:
    spec = REGISTRY[name]
    ok, err = validate_arguments(spec, args)
    if not ok:
        return ToolResult(success=False, error_code="TOOL_INVALID_ARGUMENT",
                          data={"error": err}, risk_level=spec.risk_class.value)

    # CONTRACT §28 RE-REASON: serve the recorded result, do not re-execute.
    if frozen is not None:
        return ToolResult(**{k: v for k, v in frozen.items()
                             if k in ToolResult.model_fields})

    impl = _READ_IMPL.get(name)
    if impl is None:
        return ToolResult(success=False, error_code="TOOL_UNAVAILABLE",
                          data={"error": f"{name} is not a read tool."},
                          risk_level=spec.risk_class.value)
    clean = {k: v for k, v in args.items() if v is not None}
    if name in _NEEDS_ADAPTER:
        return impl(session, merchant_id, adapter=adapter, **clean)
    return impl(session, merchant_id, **clean)
