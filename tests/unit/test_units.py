"""Unit tests for the deterministic components."""
from __future__ import annotations

import pytest

from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import Fault, FaultInjector, ProviderTimeout
from app.models import VerificationState
from app.policy.engine import Decision, PolicyContext, approval_is_valid, evaluate
from app.tools.contracts import Evidence, Finding, ToolResult
from app.tools.registry import REGISTRY, validate_arguments
from app.verification.engine import verify_refund


# ------------------------------------------------------------------ findings
def test_observed_finding_requires_resolvable_citation():
    f = Finding(claim="upi fell", kind="OBSERVED", evidence_refs=["TC_1"])
    assert f.is_grounded({"TC_1"})
    assert not f.is_grounded({"TC_2"})
    assert not Finding(claim="x", kind="OBSERVED").is_grounded({"TC_1"})


def test_inferred_finding_does_not_require_direct_citation():
    assert Finding(claim="cause is upi", kind="INFERRED").is_grounded(set())


# ------------------------------------------------------- argument validation
@pytest.mark.parametrize("args,ok", [
    ({"window_seconds": 600}, True),
    ({"window_seconds": 0}, False),          # below minimum
    ({"window_seconds": "600"}, False),      # wrong type
    ({"window_seconds": 600, "x": 1}, False),  # unknown argument
    ({}, False),                              # missing required
])
def test_validate_duplicate_args(args, ok):
    passed, _ = validate_arguments(REGISTRY["find_duplicate_payments"], args)
    assert passed is ok


def test_enum_and_null_accepted_for_method():
    spec = REGISTRY["get_payment_metrics"]
    assert validate_arguments(spec, {"method": "upi"})[0]
    assert validate_arguments(spec, {"method": None})[0]
    assert not validate_arguments(spec, {"method": "crypto"})[0]


# --------------------------------------------------------------------- risk
def test_read_tools_are_low_and_refund_is_high():
    assert REGISTRY["get_revenue_summary"].risk_class.value == "LOW"
    assert REGISTRY["request_refund"].risk_class.value == "HIGH"


def test_high_risk_always_requires_approval(db):
    ctx = PolicyContext("USR_A_OWNER", "MERCH_A", "owner",
                        ["action:refund"], "request_refund", "HIGH",
                        {"synthetic_payment_id": "SYN_PAY_0002", "amount_minor": 499900})
    assert evaluate(db, ctx).decision is Decision.REQUIRE_APPROVAL


def test_amount_limit_denies(db):
    ctx = PolicyContext("USR_A_OWNER", "MERCH_A", "owner",
                        ["action:refund"], "request_refund", "HIGH",
                        {"synthetic_payment_id": "SYN_PAY_0005", "amount_minor": 980000})
    r = evaluate(db, ctx)
    assert r.decision is Decision.DENY and r.rule == "amount_limit_exceeded"


# ---------------------------------------------------------------- approvals
def test_expired_approval_is_invalid():
    from datetime import datetime, timedelta, timezone

    class A:
        id = "APR_X"
        decision = "APPROVED"
        expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    ok, why = approval_is_valid(A())
    assert not ok and "expired" in why.lower()


def test_rejected_approval_is_invalid():
    class A:
        id = "APR_Y"
        decision = "REJECTED"
        expires_at = None

    assert not approval_is_valid(A())[0]


# ------------------------------------------------------------- verification
def test_verification_success_reads_the_payment(db):
    adapter = get_adapter(db)
    before = adapter.get_payment("pay_MOCKTEST00000002").amount_refunded_minor
    ref = adapter.create_refund("pay_MOCKTEST00000002", 499900, "k-unit-1")
    vr = verify_refund(adapter, external_payment_id="pay_MOCKTEST00000002",
                       expected_refund_minor=499900, refunded_before_minor=before,
                       external_reference=ref.id)
    assert vr.state is VerificationState.SUCCESS
    assert vr.actual["amount_refunded_minor"] == before + 499900


def test_verification_unknown_when_state_unreadable(db):
    class Blind:
        mode = "blind"
        def get_payment(self, _):
            raise ProviderTimeout("unreachable", submitted=True)
        def get_refund(self, _):
            return None
        def create_refund(self, *a, **k):
            raise NotImplementedError
        def find_refund_by_idempotency_key(self, _):
            return None

    vr = verify_refund(Blind(), external_payment_id="pay_x",
                       expected_refund_minor=100, refunded_before_minor=0,
                       external_reference="rfnd_x")
    assert vr.state is VerificationState.UNKNOWN


def test_timeout_is_never_silently_success(db):
    inj = FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT)
    adapter = get_adapter(db, inj)
    with pytest.raises(ProviderTimeout) as e:
        adapter.create_refund("pay_MOCKTEST00000001", 499900, "k-unit-2")
    assert e.value.submitted is True


# ------------------------------------------------------------- idempotency
def test_provider_replay_returns_same_refund(db):
    adapter = get_adapter(db)
    a = adapter.create_refund("pay_MOCKTEST00000003", 149900, "k-unit-3")
    b = adapter.create_refund("pay_MOCKTEST00000003", 149900, "k-unit-3")
    assert a.id == b.id
    p = adapter.get_payment("pay_MOCKTEST00000003")
    assert p.amount_refunded_minor == 149900, "replay double-refunded"


# ---------------------------------------------------------------- contracts
def test_tool_result_shape():
    r = ToolResult(success=True, data={"a": 1},
                   evidence=[Evidence(key="k", value=1, source="t")])
    d = r.model_dump()
    assert set(d) >= {"success", "data", "evidence", "external_reference", "error_code"}


def test_anthropic_tool_definition_is_strict():
    t = REGISTRY["request_refund"].to_anthropic_tool()
    assert t["strict"] is True
    assert t["input_schema"]["additionalProperties"] is False
