"""Risk engine — MerchantOps §24.

The property that matters is the floor rule. Everything else is calibration.
"""
from __future__ import annotations

import itertools

import pytest
from sqlalchemy import text

from app.models import RISK_ORDER, risk_at_least
from app.policy.risk import assess
from app.tools.registry import REGISTRY


def test_risk_order_is_ordinal_not_alphabetical():
    """Compared as strings, 'CRITICAL' < 'HIGH' and the floor rule inverts."""
    assert RISK_ORDER["CRITICAL"] > RISK_ORDER["HIGH"] > RISK_ORDER["MEDIUM"] > RISK_ORDER["LOW"]
    assert risk_at_least("HIGH", "CRITICAL") == "CRITICAL"
    assert "CRITICAL" < "HIGH"          # the trap this guards against


@pytest.mark.parametrize("a,b", list(itertools.product(RISK_ORDER, repeat=2)))
def test_risk_at_least_is_the_maximum(a, b):
    assert RISK_ORDER[risk_at_least(a, b)] == max(RISK_ORDER[a], RISK_ORDER[b])


@pytest.mark.parametrize("declared", list(RISK_ORDER))
def test_assessment_never_falls_below_the_declared_class(db, declared):
    """THE floor rule. Computed risk may raise a call; it may never lower one.

    Arguments come from the model. If a computed score could lower risk, then
    model-supplied input would have a path to weaken a control — an injected
    instruction that made an action merely *look* small would buy a softer gate.
    """
    spec = REGISTRY["request_refund"]
    a = assess(db, tool_name="request_refund", declared=declared,
               merchant_id="MERCH_A", arguments={"amount_minor": 1}, spec=spec)
    assert RISK_ORDER[a.level] >= RISK_ORDER[declared], (
        f"declared {declared} was lowered to {a.level}")
    assert a.declared == declared


def test_a_tiny_amount_does_not_soften_a_high_risk_tool(db):
    spec = REGISTRY["request_refund"]
    a = assess(db, tool_name="request_refund", declared="HIGH",
               merchant_id="MERCH_A", arguments={"amount_minor": 100}, spec=spec)
    assert a.level == "HIGH"
    assert not a.was_raised


def test_value_alone_never_reaches_critical(db):
    """§24 grades a INR 5,000 refund — merchant A's whole limit — as HIGH, and
    reserves CRITICAL for bulk. Value is the most serious ordinary input, not
    an extraordinary one."""
    spec = REGISTRY["request_refund"]
    limit = db.execute(text(
        "SELECT policy_config->>'refund_limit_minor' FROM merchants WHERE id='MERCH_A'"
    )).scalar()
    a = assess(db, tool_name="request_refund", declared="HIGH",
               merchant_id="MERCH_A", arguments={"amount_minor": int(limit)}, spec=spec)
    assert a.level == "HIGH"
    assert any(f.name == "financial_value" for f in a.factors)


def test_irreversibility_is_recorded_as_a_factor(db):
    a = assess(db, tool_name="request_refund", declared="HIGH", merchant_id="MERCH_A",
               arguments={"amount_minor": 100}, spec=REGISTRY["request_refund"])
    assert any(f.name == "irreversibility" for f in a.factors)


def test_an_unsettled_prior_action_raises_to_critical(db, owner):
    """The one path to CRITICAL today, and a real one: the duplicate-action
    guard does not block a payment whose previous action is UNKNOWN, which is
    exactly where a double refund could happen."""
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.models import VerificationState

    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]
    assert action.verification_state is VerificationState.UNKNOWN

    a = assess(db, tool_name="request_refund", declared="HIGH", merchant_id="MERCH_A",
               arguments={"synthetic_payment_id": action.target_payment_id,
                          "amount_minor": 100},
               spec=REGISTRY["request_refund"])
    assert a.level == "CRITICAL"
    assert a.was_raised
    assert any(f.name == "uncertainty" for f in a.factors)


def test_registry_is_the_only_declaration_of_risk_and_permissions():
    """The engine used to declare both again in its own dicts, and silently
    win. Nine more tools are due; the duplication is retired before they land."""
    import app.policy.engine as engine

    assert not hasattr(engine, "TOOL_RISK")
    assert not hasattr(engine, "TOOL_PERMISSIONS")
    for name, spec in REGISTRY.items():
        assert engine.declared_risk(name) == spec.risk_class.value
        assert engine.required_permissions(name) == list(spec.required_permissions)
