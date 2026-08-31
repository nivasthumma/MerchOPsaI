"""The three carried-forward items — MerchantOps §11, §49, §57.

Each was documented as open in its own ADR. What they had in common is that a
rule existed in one place and was restated, or not consulted, in another.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime
from app.config import get_settings
from app.detection import detect
from app.detection.rules import BURST_THRESHOLD, detect_provider_failure_burst
from app.failures import Retryability, classify, may_retry, should_reconcile, unsettled_states
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.models import (
    ActionStatus, AgentAction, AgentTask, CandidateStatus, Incident, IncidentType,
    TaskStatus, VerificationState, WebhookStatus,
)
from app.tools.actions import REVERIFIERS, reverify_action
from app.tools.recovery_actions import execute_payment_link
from app.verification.reconciler import UNSETTLED, reconcile
from app.webhooks import ingest

SECRET = "whsec_closing_three"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", SECRET, raising=False)
    return SECRET


def _failed_payment(db) -> str:
    return db.execute(text(
        "SELECT id FROM payments WHERE merchant_id='MERCH_A' AND status='failed' "
        "ORDER BY id LIMIT 1")).scalar()


def _link_action(db, owner, *, lose_the_reference: bool = False) -> AgentAction:
    task = AgentTask(id=f"TASK_{uuid.uuid4().hex[:10].upper()}", merchant_id="MERCH_A",
                     user_id=owner.user_id, request="t", status=TaskStatus.COMPLETED,
                     agent_version="t", model_version="t", prompt_version="t")
    db.add(task)
    db.flush()
    out = execute_payment_link(db, get_adapter(db), task_id=task.id,
                               merchant_id="MERCH_A", approval_id=f"APR_{uuid.uuid4().hex[:6]}",
                               synthetic_payment_id=_failed_payment(db))
    a = out.action
    if lose_the_reference:
        a.external_reference = None
        a.verification_state = VerificationState.UNKNOWN
        a.status = ActionStatus.UNKNOWN
        db.flush()
    return a


# ------------------------------------------------------------------- §57
def test_the_sweep_takes_its_unsettled_states_from_the_taxonomy():
    """Two copies of one rule is one copy too many — they disagreed about
    VERIFICATION_FAILED and neither was in a position to notice."""
    assert sorted(s.value for s in UNSETTLED) == list(unsettled_states())
    assert should_reconcile("UNKNOWN") and should_reconcile("PARTIAL")
    # A determination is not reconcilable. Re-reading a verified failure
    # changes nothing, and re-issuing the action is forbidden.
    assert not should_reconcile("FAILED")
    assert not should_reconcile("SUCCESS")
    assert classify("VERIFICATION_FAILED").retryability is Retryability.ESCALATE


def test_a_read_tool_retries_only_a_transient_failure(db):
    """`max_retries` had been on ToolSpec since the first version and nothing
    read it, so a tool could declare a budget and never get one."""
    from app.tools import registry as reg
    from app.tools.contracts import ToolResult

    assert reg.REGISTRY["get_payment_status"].max_retries == 2

    calls = {"n": 0}

    def flaky(session, merchant_id, adapter=None, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return ToolResult(success=False, error_code="EXTERNAL_API_ERROR",
                              data={}, risk_level="LOW")
        return ToolResult(success=True, data={"ok": True}, risk_level="LOW")

    original = reg._READ_IMPL["get_payment_status"]
    reg._READ_IMPL["get_payment_status"] = flaky
    try:
        r = reg.execute_read_tool(db, "get_payment_status", "MERCH_A",
                                  {"payment_id": "SYN_PAY_0002"})
    finally:
        reg._READ_IMPL["get_payment_status"] = original
    assert r.success and calls["n"] == 3


def test_a_non_transient_failure_is_never_retried(db):
    """A policy denial gets zero attempts no matter what a spec declares: the
    answer will be identical."""
    from app.tools import registry as reg
    from app.tools.contracts import ToolResult

    calls = {"n": 0}

    def denied(session, merchant_id, adapter=None, **kw):
        calls["n"] += 1
        return ToolResult(success=False, error_code="POLICY_DENIED", data={},
                          risk_level="LOW")

    original = reg._READ_IMPL["get_payment_status"]
    reg._READ_IMPL["get_payment_status"] = denied
    try:
        reg.execute_read_tool(db, "get_payment_status", "MERCH_A",
                              {"payment_id": "SYN_PAY_0002"})
    finally:
        reg._READ_IMPL["get_payment_status"] = original
    assert calls["n"] == 1
    assert may_retry("POLICY_DENIED") is False


# ------------------------------------------- the UNKNOWN exit path, per type
def test_every_action_type_has_a_reverifier():
    from app.agent.approval import EXECUTORS

    produced = {"request_refund": "refund", "generate_payment_link": "payment_link",
                "send_customer_notification": "notification"}
    assert set(produced) == set(EXECUTORS)
    for action_type in produced.values():
        assert action_type in REVERIFIERS, f"{action_type} cannot be reconciled"


def test_the_sweep_settles_a_payment_link_whose_response_was_lost(db, owner):
    """It could not, before. Reconciling a link asked the provider about a
    PAYMENT with an empty id, got "Payment  could not be retrieved", and left
    the action UNKNOWN forever — the UNKNOWN exit path worked for exactly one
    of three action types."""
    a = _link_action(db, owner, lose_the_reference=True)
    assert a.verification_state is VerificationState.UNKNOWN

    reconcile(db, min_age_seconds=0)
    db.refresh(a)
    assert a.verification_state is VerificationState.SUCCESS
    # Recovered from our own idempotency key, which is the only handle we kept.
    assert a.external_reference.startswith("plink_")
    assert "payment link" in a.verification_detail["reason"].lower()


def test_an_unreverifiable_action_says_so_rather_than_guessing(db, owner):
    a = _link_action(db, owner)
    a.action_type = "some_future_action"
    db.flush()
    vr = reverify_action(db, get_adapter(db), a)
    assert vr.state is VerificationState.UNKNOWN
    assert "no re-verifier" in vr.reason.lower()


# ------------------------------------------------------------------- §49
def _deliver(db, event: str, entity_id: str, event_id: str):
    body = {"entity": "event", "event": event, "created_at": 1787000000,
            "payload": {"payment_link": {"entity": {"id": entity_id}}}}
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return ingest(db, raw, sig, event_id)


def test_a_paid_link_is_recorded_as_recovered_when_the_provider_says_so(
        db, owner, secret):
    """It was discovered only when a plan happened to be settled, so recovered
    revenue lagged reality by however long it took someone to ask."""
    from app.recovery import plan_recovery
    from app.recovery.dispatch import dispatch_candidate, executable_candidates

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.PAYMENT_DEGRADATION).first()
    plan = plan_recovery(db, inc).plan
    for c in executable_candidates(db, plan)[1:]:
        c.executable = False
    db.flush()
    cand = executable_candidates(db, plan)[0]

    out = dispatch_candidate(db, plan, cand, owner)
    res = approve_and_execute(db, out["task"].id, owner)
    link_id = res["action"].external_reference
    res["action"].recovery_candidate_id = cand.id
    db.flush()

    db.refresh(cand)
    assert cand.actual_recovery_minor == 0

    # The customer pays, and the provider tells us.
    db.execute(text("UPDATE payment_links SET status='paid' WHERE id = :i"), {"i": link_id})
    db.flush()
    result = _deliver(db, "payment_link.paid", link_id, "evt_paid_1")

    assert result.status is WebhookStatus.PROCESSED
    assert "settled plan" in result.note
    db.refresh(cand)
    assert cand.status is CandidateStatus.RECOVERED
    assert cand.actual_recovery_minor == cand.attributed_amount_minor


def test_a_link_event_matches_the_link_not_the_payment_that_settled_it(db, owner, secret):
    """A payment_link event's `payment` entity is the payment that settled it.
    Matching on that would reconcile the wrong action."""
    from app.webhooks.razorpay import _extract

    body = {"entity": "event", "event": "payment_link.paid", "created_at": 1,
            "payload": {"payment_link": {"entity": {"id": "plink_X"}},
                        "payment": {"entity": {"id": "pay_SOMETHING_ELSE"}}}}
    _, entity_id, _ = _extract(body)
    assert entity_id == "plink_X"


# ------------------------------------------------------------------- §11
def _plant_events(db, n: int, event_type: str = "payment.failed", spread_minutes: int = 5):
    for i in range(n):
        db.execute(text("""
            INSERT INTO webhook_events (id, event_id, provider, event_type, schema_version,
                                        tenant_id, merchant_id, entity_id, status,
                                        signature_valid, payload, payload_hash,
                                        correlation_id, occurred_at, received_at)
            VALUES (:id, :eid, 'razorpay', :et, 'v1', 'TEN_KETTLE', 'MERCH_A', :ent,
                    'PROCESSED', true, '{}', 'h', 'COR_X',
                    now() - (:off || ' minutes')::interval, now())
        """), {"id": f"WHE_{i:04d}", "eid": f"evt_burst_{event_type}_{i}", "et": event_type,
               "ent": f"pay_{i}", "off": (spread_minutes * i) // max(n - 1, 1)})
    db.flush()


def test_detection_reads_the_event_store_not_only_payment_history(db):
    """The other rules read our own records. This one reads what the PROVIDER
    said, which is the reason §11 puts a durable event store in front of
    detection at all."""
    _plant_events(db, BURST_THRESHOLD)
    found = detect_provider_failure_burst(db, "MERCH_A")
    assert len(found) == 1
    a = found[0]
    assert a.incident_type is IncidentType.PROVIDER_FAILURE_BURST
    assert a.signals["source"] == "webhook_events"
    assert a.signals["event_count"] == BURST_THRESHOLD
    # No revenue figure: the events name entities, not amounts, and inventing
    # an exposure from a count is what §22 forbids.
    assert a.revenue_at_risk_minor == 0


def test_a_burst_below_the_threshold_is_not_an_incident(db):
    _plant_events(db, BURST_THRESHOLD - 1)
    assert detect_provider_failure_burst(db, "MERCH_A") == []


def test_unverified_events_are_not_evidence(db):
    """An unsigned delivery is stored for investigation and is not evidence of
    anything (§34)."""
    _plant_events(db, BURST_THRESHOLD + 3)
    db.execute(text("UPDATE webhook_events SET signature_valid = false"))
    db.flush()
    assert detect_provider_failure_burst(db, "MERCH_A") == []


def test_the_burst_rule_runs_in_the_sweep_and_is_idempotent(db):
    _plant_events(db, BURST_THRESHOLD)
    first = detect(db, "MERCH_A")
    kinds = {i.incident_type for i in db.query(Incident).all()}
    assert IncidentType.PROVIDER_FAILURE_BURST in kinds

    second = detect(db, "MERCH_A")
    assert second.incidents_created == 0
    assert second.already_known == first.incidents_created


def test_a_provider_burst_is_escalated_rather_than_acted_on(db):
    """Not something this system can remedy by acting on transactions."""
    from app.models import Intervention
    from app.recovery import plan_recovery

    _plant_events(db, BURST_THRESHOLD)
    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.PROVIDER_FAILURE_BURST).one()
    plan = plan_recovery(db, inc).plan
    assert plan.intervention is Intervention.HUMAN_ESCALATION
