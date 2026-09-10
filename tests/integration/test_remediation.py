"""The architecture remediation (ADR-0053), under test.

Each section is one invariant the review named, stated as a property of the
running system rather than of a function in isolation:

    the database scope survives a commit
    no provider call with writes held open
    the live provider path settles ambiguity by reading, never by retrying
    UNKNOWN is never retried
    same key + different request is a conflict
    an approval pins the request it approved; revoked and replayed approvals
        never execute
    a replay never reaches the provider
    AI fallback is recorded, never disguised
    every audit row says who acted, by kind
    domain facts are events, and not timeline frames
    a link created is not revenue recovered
    no action is hidden from the Action Center
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import context, tenancy
from app.agent.approval import ApprovalError, approve_and_execute, revoke
from app.agent.provenance import AIMode
from app.agent.replay import re_reason
from app.agent.runtime import AgentRuntime, AgentRuntimeError
from app.api import security as sec
from app.api.main import app
from app.boundaries import VIOLATIONS, BoundaryViolation, open_write
from app.config import get_settings
from app.db import checkpoint, get_engine
from app.idempotency import IdempotencyConflict, claim, request_hash
from app.integrations.razorpay.adapter import LiveTestModeAdapter, MockAdapter, get_adapter
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.llm.base import LLMProvider, LLMTurn, ModelUnavailable, ToolRequest
from app.llm.deterministic import DeterministicProvider
from app.models import (
    ActionStatus,
    AgentAction,
    AuditLog,
    CandidateStatus,
    EventOutbox,
    IdempotencyRecord,
    Refund,
    TaskStatus,
    ToolCall,
    VerificationState,
)
from app.policy.engine import POLICY_VERSION, approval_is_valid
from app.recovery.dispatch import _settle_one
from app.tools.actions import execute_refund, reverify_action
from app.tools.recovery_actions import verify_payment_link
from app.verification.reconciler import reconcile

REFUND_REQUEST = "Refund the duplicate payment SYN_PAY_0002 amount 499900."


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def _proposal(db, owner):
    out = AgentRuntime(db, owner).run(REFUND_REQUEST)
    assert out.status is TaskStatus.AWAITING_APPROVAL, out.answer
    return out.task, out.approval


def _count_provider_refunds(monkeypatch) -> dict:
    """Count every create_refund that reaches the (mock) provider."""
    calls = {"n": 0}
    original = MockAdapter.create_refund

    def counted(self, *a, **k):
        calls["n"] += 1
        return original(self, *a, **k)
    monkeypatch.setattr(MockAdapter, "create_refund", counted)
    return calls


# ======================================================== tenancy after commit
def test_the_database_scope_survives_a_mid_request_commit(_seeded_schema):
    """`checkpoint()` commits mid-request. The scope is SET LOCAL, so it used to
    die with that commit, and everything after it ran unrestricted."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)

    with tenancy.unscoped():
        s = factory()
        try:
            everyone = s.execute(text(
                "SELECT COUNT(*) FROM payments WHERE merchant_id = 'MERCH_B'")).scalar()
        finally:
            s.close()
    assert everyone > 0, "the check below would be vacuous"

    with tenancy.scoped("TEN_KETTLE", "MERCH_A"):
        s = factory()
        try:
            def scope():
                return s.execute(text(
                    "SELECT current_setting('app.merchant_id', true)")).scalar()
            assert scope() == "MERCH_A"
            checkpoint(s)
            assert scope() == "MERCH_A", "a commit shed the row-level-security scope"
            leaked = s.execute(text(
                "SELECT COUNT(*) FROM payments WHERE merchant_id = 'MERCH_B'")).scalar()
            assert leaked == 0
        finally:
            s.rollback()
            s.close()


def test_background_work_that_declares_a_merchant_is_narrowed_to_it(_seeded_schema):
    """A transaction opened before the scope was bound -- the worker's case --
    is narrowed by `context.bound`, not only transactions begun afterwards."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    s = factory()
    try:
        s.connection()                               # begun unscoped
        ctx = context.ExecutionContext(context.ActorType.WORKER, actor="w1",
                                       tenant_id="TEN_KETTLE", merchant_id="MERCH_A")
        with context.bound(ctx, s):
            assert s.execute(text(
                "SELECT current_setting('app.merchant_id', true)")).scalar() == "MERCH_A"
            assert context.current().actor_type is context.ActorType.WORKER
    finally:
        s.rollback()
        s.close()


# ==================================================== the transaction boundary
def test_a_provider_call_with_writes_open_is_refused(db):
    adapter = get_adapter(db)
    db.execute(text("UPDATE merchants SET name = name WHERE id = 'MERCH_A'"))
    assert open_write(db)
    with pytest.raises(BoundaryViolation):
        adapter.get_payment("pay_MOCKTEST00000002")
    db.commit()
    assert not open_write(db)
    assert adapter.get_payment("pay_MOCKTEST00000002") is not None


def test_reads_alone_do_not_trip_the_guard(db):
    db.commit()
    db.execute(text("SELECT COUNT(*) FROM payments"))
    assert not open_write(db)
    assert get_adapter(db).get_payment("pay_MOCKTEST00000002") is not None


def test_warn_mode_records_a_violation_without_raising(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "transaction_boundary_mode", "warn")
    VIOLATIONS.clear()
    db.execute(text("UPDATE merchants SET name = name WHERE id = 'MERCH_A'"))
    get_adapter(db).get_payment("pay_MOCKTEST00000002")
    assert VIOLATIONS and VIOLATIONS[-1]["call"] == "provider.get_payment"


def test_the_whole_approval_path_runs_clean_under_strict_mode(db, owner):
    """Proposal, approval, execution and verification with the guard raising:
    every provider call in the path is preceded by a commit."""
    assert get_settings().transaction_boundary_mode == "strict"
    task, _ = _proposal(db, owner)
    r = approve_and_execute(db, task.id, owner)
    assert r["action"].verification_state is VerificationState.SUCCESS


# ======================================= the live provider path, end to end
def _payment(db, pid="SYN_PAY_0002"):
    return db.execute(text("""
        SELECT external_payment_id AS ext, amount_minor AS amount,
               amount_refunded_minor AS refunded
          FROM payments WHERE id = :p"""), {"p": pid}).mappings().one()


def _live(db, handler):
    posts = {"n": 0}

    def wrapped(request):
        if request.method == "POST":
            posts["n"] += 1
        return handler(request)
    client = httpx.Client(base_url=LiveTestModeAdapter.BASE,
                          transport=httpx.MockTransport(wrapped))
    return LiveTestModeAdapter(db, client=client), posts


def _execute_live(db, owner, handler):
    task, ap = _proposal(db, owner)
    adapter, posts = _live(db, handler)
    out = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                         synthetic_payment_id=ap.action_payload["synthetic_payment_id"],
                         amount_minor=int(ap.action_payload["amount_minor"]),
                         approval_id=ap.id)
    return out, adapter, posts


def test_a_5xx_that_did_apply_is_settled_by_our_key_not_by_a_retry(db, owner):
    """Razorpay answered 503, but the refund exists under our key. The path must
    find it by reading, settle SUCCESS, and never send the refund twice."""
    pay = _payment(db)
    amt = 499900
    seen = {"key": None}

    def handler(req):
        path = req.url.path
        if req.method == "POST":
            seen["key"] = req.headers["X-Refund-Idempotency"]
            return httpx.Response(503, json={"error": {"code": "SERVER_ERROR",
                                                       "description": "upstream"}})
        if path == f"/v1/payments/{pay['ext']}/refunds":
            return httpx.Response(200, json={"entity": "collection", "count": 1, "items": [
                {"id": "rfnd_LIVE1", "amount": amt, "payment_id": pay["ext"],
                 "notes": {"idempotency_key": seen["key"]}, "status": "processed"}]})
        if path == f"/v1/payments/{pay['ext']}":
            return httpx.Response(200, json={
                "id": pay["ext"], "amount": pay["amount"],
                "amount_refunded": pay["refunded"] + amt,
                "refund_status": "partial", "status": "captured"})
        if path == "/v1/refunds/rfnd_LIVE1":
            return httpx.Response(200, json={"id": "rfnd_LIVE1", "amount": amt,
                                             "payment_id": pay["ext"], "status": "processed"})
        return httpx.Response(404)

    out, _, posts = _execute_live(db, owner, handler)
    assert posts["n"] == 1, "the refund was sent more than once"
    assert out.result.success is True
    assert out.action.status is ActionStatus.CONFIRMED
    assert out.action.verification_state is VerificationState.SUCCESS
    assert out.action.external_reference == "rfnd_LIVE1"


def test_a_409_in_flight_is_unknown_and_reconciliation_only_reads(db, owner):
    pay = _payment(db)

    def handler(req):
        path = req.url.path
        if req.method == "POST":
            return httpx.Response(409, json={"error": {
                "code": "BAD_REQUEST_ERROR", "description": "request in progress"}})
        if path == f"/v1/payments/{pay['ext']}/refunds":
            return httpx.Response(200, json={"entity": "collection", "count": 0, "items": []})
        if path == f"/v1/payments/{pay['ext']}":
            return httpx.Response(200, json={
                "id": pay["ext"], "amount": pay["amount"],
                "amount_refunded": pay["refunded"], "status": "captured"})
        return httpx.Response(404)

    out, adapter, posts = _execute_live(db, owner, handler)
    assert out.result.error_code == "EXTERNAL_STATE_UNKNOWN"
    assert out.action.status is ActionStatus.UNKNOWN
    assert out.action.verification_state is VerificationState.UNKNOWN

    reverify_action(db, adapter, out.action)
    assert posts["n"] == 1, "reconciliation re-sent a refund whose outcome was unknown"


def test_a_definite_refusal_is_failed_not_unknown(db, owner):
    pay = _payment(db)

    def handler(req):
        if req.method == "POST":
            return httpx.Response(400, json={"error": {
                "code": "BAD_REQUEST_ERROR",
                "description": "The refund amount provided is greater than amount captured"}})
        if req.url.path == f"/v1/payments/{pay['ext']}":
            return httpx.Response(200, json={"id": pay["ext"], "amount": pay["amount"],
                                             "amount_refunded": 0, "status": "captured"})
        return httpx.Response(404)

    out, _, posts = _execute_live(db, owner, handler)
    assert posts["n"] == 1
    assert out.action.status is ActionStatus.FAILED
    assert out.action.verification_state is VerificationState.FAILED
    assert "greater than amount captured" in out.result.data["error"]


# ================================================================ UNKNOWN
def test_a_timeout_before_submit_is_failed_and_says_timeout(db, owner, monkeypatch):
    """Nothing was sent, so nothing is unknown. It used to leave the action
    status UNKNOWN beside a FAILED verification -- in no queue at all."""
    calls = _count_provider_refunds(monkeypatch)
    task, _ = _proposal(db, owner)
    r = approve_and_execute(db, task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_BEFORE_SUBMIT))
    assert calls["n"] == 1
    assert r["action"].status is ActionStatus.FAILED
    assert r["action"].verification_state is VerificationState.FAILED
    assert r["result"].error_code == "TOOL_TIMEOUT"


def test_an_unknown_refund_is_never_retried_by_reconciliation(db, owner, monkeypatch):
    calls = _count_provider_refunds(monkeypatch)
    task, _ = _proposal(db, owner)
    refunds_before = db.query(Refund).count()
    r = approve_and_execute(db, task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    assert r["action"].verification_state is VerificationState.UNKNOWN
    assert calls["n"] == 1
    db.commit()

    for _ in range(3):
        reconcile(db, min_age_seconds=0, respect_backoff=False)
    assert calls["n"] == 1, "reconciliation re-issued an action whose outcome was unknown"
    # Exactly the one refund the provider applied before the response was lost.
    assert db.query(Refund).count() == refunds_before + 1


# ============================================================ idempotency
def test_the_same_key_with_a_different_request_is_a_conflict(db):
    first = claim(db, merchant_id="MERCH_A", operation="op.test", business_key="k-1",
                  request={"amount_minor": 100, "payment": "P"})
    assert first.fresh
    again = claim(db, merchant_id="MERCH_A", operation="op.test", business_key="k-1",
                  request={"payment": "P", "amount_minor": 100})
    assert not again.fresh and again.record.id == first.record.id
    with pytest.raises(IdempotencyConflict):
        claim(db, merchant_id="MERCH_A", operation="op.test", business_key="k-1",
              request={"amount_minor": 999, "payment": "P"})


def test_key_order_is_not_a_difference():
    assert request_hash({"a": 1, "b": [1, 2]}) == request_hash({"b": [1, 2], "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_a_changed_refund_under_a_reused_key_never_reaches_the_provider(
        db, owner, monkeypatch):
    task, ap = _proposal(db, owner)
    pid = ap.action_payload["synthetic_payment_id"]
    adapter = get_adapter(db)
    first = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                           synthetic_payment_id=pid, amount_minor=1000, approval_id=ap.id)
    assert first.result.success

    record = db.query(IdempotencyRecord).filter(
        IdempotencyRecord.resource_id == first.action.id).one()
    assert record.operation == "action.refund"
    assert record.status == "SUCCEEDED"
    assert record.external_reference == first.action.external_reference

    calls = _count_provider_refunds(monkeypatch)
    changed = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                             synthetic_payment_id=pid, amount_minor=2000, approval_id=ap.id)
    assert changed.result.error_code == "IDEMPOTENCY_CONFLICT"
    assert changed.action is None and calls["n"] == 0

    same = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                          synthetic_payment_id=pid, amount_minor=1000, approval_id=ap.id)
    assert same.result.data["error"] == "duplicate_action"
    assert calls["n"] == 0, "a replay of the same request reached the provider"


# =============================================================== approvals
def test_an_approval_records_the_policy_it_was_granted_under(db, owner):
    task, ap = _proposal(db, owner)
    assert ap.policy_version == POLICY_VERSION
    assert ap.policy_decision == "REQUIRE_APPROVAL"
    assert ap.policy_input_hash and len(ap.policy_input_hash) == 64

    approve_and_execute(db, task.id, owner)
    recheck = db.query(AuditLog).filter(AuditLog.task_id == task.id,
                                        AuditLog.event_type == "policy_recheck").one()
    assert recheck.payload["approved_under_policy"] == POLICY_VERSION
    assert recheck.payload["executing_under_policy"] == POLICY_VERSION


def test_a_payload_changed_after_approval_does_not_execute(db, owner):
    task, ap = _proposal(db, owner)
    ap.action_payload = {**ap.action_payload, "amount_minor": 100}
    db.flush()
    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, task.id, owner)
    assert e.value.code == "POLICY_DENIED"
    assert db.query(AgentAction).filter(AgentAction.task_id == task.id).count() == 0


def test_a_revoked_approval_never_executes(db, owner, client):
    task, ap = _proposal(db, owner)
    revoke(db, task.id, owner, reason="changed my mind")
    db.refresh(ap)
    assert ap.decision == "REVOKED"
    assert approval_is_valid(ap)[0] is False
    with pytest.raises(ApprovalError):
        approve_and_execute(db, task.id, owner)
    assert db.query(AgentAction).filter(AgentAction.task_id == task.id).count() == 0


def test_revoke_is_an_endpoint(db, owner, client):
    task, _ = _proposal(db, owner)
    db.commit()
    r = client.post(f"/tasks/{task.id}/revoke", headers=token("USR_A_OWNER"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"
    again = client.post(f"/tasks/{task.id}/approve", headers=token("USR_A_OWNER"))
    assert again.status_code == 409


# ================================================================== replay
def test_a_replays_approval_can_never_be_executed(db, owner):
    task, _ = _proposal(db, owner)
    out = re_reason(db, task.id, owner)
    replay_id = out["replay_task_id"]
    with pytest.raises(ApprovalError) as e:
        approve_and_execute(db, replay_id, owner)
    assert e.value.code == "AUTHORIZATION_DENIED"
    assert db.query(AgentAction).filter(AgentAction.task_id == replay_id).count() == 0


class Scripted(LLMProvider):
    """A provider that says exactly what the test tells it to."""
    name = "scripted"
    model = "scripted-1"

    def __init__(self, turns):
        self._turns = list(turns)

    def turn(self, *, system, messages, tools, timeout=None):
        return self._turns.pop(0)


def _reads_payment_status():
    return [LLMTurn(tool_requests=[ToolRequest(
                id="tu_1", name="get_payment_status",
                arguments={"payment_id": "SYN_PAY_0002"})], stop_reason="tool_use"),
            LLMTurn(text="Checked.")]


def test_a_replay_never_calls_the_provider_for_a_read(db, owner, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("a replay reached the provider")
    monkeypatch.setattr(MockAdapter, "get_payment", forbidden)

    out = AgentRuntime(db, owner, provider=Scripted(_reads_payment_status()),
                       frozen_tools={}).run("Check SYN_PAY_0002 at the provider.",
                                            is_replay=True)
    tc = db.query(ToolCall).filter(ToolCall.task_id == out.task.id,
                                   ToolCall.tool_name == "get_payment_status").one()
    assert tc.error_code == "TOOL_UNAVAILABLE"
    assert "never calls the provider" in tc.output["error"]


def test_a_live_run_reads_the_provider_after_committing(db, owner):
    """The same read outside a replay reaches the adapter -- and does so under
    the strict boundary guard, so the runtime committed before calling out."""
    out = AgentRuntime(db, owner, provider=Scripted(_reads_payment_status())).run(
        "Check SYN_PAY_0002 at the provider.")
    tc = db.query(ToolCall).filter(ToolCall.task_id == out.task.id,
                                   ToolCall.tool_name == "get_payment_status").one()
    assert tc.success is True


# =============================================================== AI modes
class _Unavailable(LLMProvider):
    name = "anthropic"
    model = "claude-opus-5"

    def turn(self, **kw):
        raise ModelUnavailable("APITimeoutError: Request timed out.")


class _FailsAfterOneTurn(LLMProvider):
    """A model that answers once, then drops the connection mid-run."""
    name = "anthropic"
    model = "claude-opus-5"

    def __init__(self):
        self.turns = 0

    def turn(self, **kw):
        self.turns += 1
        if self.turns == 1:
            return LLMTurn(tool_requests=[ToolRequest(
                id="tu_1", name="get_payment_status",
                arguments={"payment_id": "SYN_PAY_0002"})], stop_reason="tool_use")
        raise ModelUnavailable("APIConnectionError: connection reset")


def test_the_planner_by_design_is_deterministic_only(db, owner):
    out = AgentRuntime(db, owner, provider=DeterministicProvider()).run("Why did revenue drop?")
    assert out.task.ai_mode == AIMode.DETERMINISTIC_ONLY.value
    assert out.task.configuration_version.startswith("cfg-")


def test_a_model_that_fails_mid_run_is_a_recorded_fallback(db, owner):
    out = AgentRuntime(db, owner, provider=_FailsAfterOneTurn()).run("Why did revenue drop?")
    assert out.task.status is TaskStatus.COMPLETED
    assert out.task.ai_mode == AIMode.AI_FAILED_FALLBACK.value
    # What was configured is still on the record; what ran is in ai_mode.
    assert out.task.model_provider == "anthropic"
    fb = db.query(AuditLog).filter(AuditLog.task_id == out.task.id,
                                   AuditLog.event_type == "llm_fallback").one()
    assert fb.payload["from_provider"] == "anthropic"
    assert fb.payload["mode"] == "AI_FAILED_FALLBACK"


def test_a_model_that_fails_on_its_first_turn_was_never_available(db, owner):
    """Nothing came from the model, so the whole run is the planner's: that is
    unavailability, not a failure partway through."""
    out = AgentRuntime(db, owner, provider=_Unavailable()).run("Why did revenue drop?")
    assert out.task.ai_mode == AIMode.AI_UNAVAILABLE_FALLBACK.value
    fb = db.query(AuditLog).filter(AuditLog.task_id == out.task.id,
                                   AuditLog.event_type == "llm_fallback").one()
    assert fb.payload["mode"] == "AI_UNAVAILABLE_FALLBACK"


def test_a_model_that_cannot_be_reached_is_an_unavailable_fallback(db, owner, monkeypatch):
    def no_model():
        raise RuntimeError("no credentials")
    monkeypatch.setattr("app.agent.runtime.get_provider", no_model)
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    assert out.task.ai_mode == AIMode.AI_UNAVAILABLE_FALLBACK.value
    assert db.query(AuditLog).filter(AuditLog.task_id == out.task.id,
                                     AuditLog.event_type == "llm_fallback").count() == 1


def test_with_fallback_disabled_an_unavailable_model_fails_the_run(db, owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_fallback_enabled", False)
    with pytest.raises(AgentRuntimeError) as e:
        AgentRuntime(db, owner, provider=_Unavailable()).run("Why did revenue drop?")
    db.rollback()
    assert e.value.task_id is not None


def test_our_own_bug_is_not_passed_off_as_model_unavailability(db, owner):
    class Buggy(_Unavailable):
        def turn(self, **kw):
            raise KeyError("a bug, not an outage")
    with pytest.raises(AgentRuntimeError):
        AgentRuntime(db, owner, provider=Buggy()).run("Why did revenue drop?")


def test_the_api_shows_how_a_run_was_produced(db, owner, client):
    out = AgentRuntime(db, owner, provider=_FailsAfterOneTurn()).run("Why did revenue drop?")
    db.commit()
    body = client.get(f"/tasks/{out.task.id}", headers=token("USR_A_OWNER")).json()
    assert body["ai_mode"] == "AI_FAILED_FALLBACK"
    assert body["versions"]["configuration"].startswith("cfg-")


# ================================================================== actors
def test_every_audit_row_says_who_acted_by_kind(db, owner, client):
    r = client.post("/tasks", json={"request": REFUND_REQUEST},
                    headers=token("USR_A_OWNER"))
    assert r.status_code < 300, r.text
    task_id = r.json()["id"]
    created = db.query(AuditLog).filter(AuditLog.task_id == task_id,
                                        AuditLog.event_type == "task_created").one()
    assert created.actor_type == "AGENT" and created.actor == f"agent:{task_id}"

    a = client.post(f"/tasks/{task_id}/approve", headers=token("USR_A_OWNER"))
    assert a.status_code == 200, a.text
    granted = db.query(AuditLog).filter(AuditLog.task_id == task_id,
                                        AuditLog.event_type == "approval_granted").one()
    assert granted.actor_type == "HUMAN" and granted.actor == "USR_A_OWNER"

    trace = client.get(f"/tasks/{task_id}/trace", headers=token("USR_A_OWNER")).json()
    assert {e["actor_type"] for e in trace["trace"]} >= {"AGENT", "HUMAN"}


def test_work_with_no_declared_actor_is_system(db):
    from app.detection.engine import detect
    detect(db, "MERCH_A")
    rows = db.query(AuditLog).filter(AuditLog.event_type == "incident_detected").all()
    assert rows and {r.actor_type for r in rows} == {"SYSTEM"}


# ================================================================== events
def test_a_refund_writes_its_domain_events_and_none_reach_the_timeline(db, owner, client):
    task, _ = _proposal(db, owner)
    approve_and_execute(db, task.id, owner)
    rows = db.query(EventOutbox).filter(EventOutbox.task_id == task.id,
                                        EventOutbox.category == "DOMAIN").all()
    assert {r.event_type for r in rows} == {
        "refund.requested", "refund.submitted", "refund.verified"}
    db.commit()
    frames = client.get("/events", headers=token("USR_A_OWNER")).json()["events"]
    assert not [f for f in frames if f["event"].startswith("refund.")]


def test_a_domain_event_rolls_back_with_its_mutation(db):
    from app.events.bus import publish
    db.commit()
    sp = db.begin_nested()
    db.execute(text("UPDATE merchants SET name = name WHERE id = 'MERCH_A'"))
    publish(db, "recovery.completed", merchant_id="MERCH_A", payload={"plan_id": "P"})
    sp.rollback()
    assert db.query(EventOutbox).filter(
        EventOutbox.event_type == "recovery.completed").count() == 0


# ======================================================= financial outcome
def _cand(attributed=1000):
    return SimpleNamespace(status=CandidateStatus.ATTEMPTED, actual_recovery_minor=0,
                           attributed_amount_minor=attributed)


def _link_action(state=VerificationState.SUCCESS):
    return SimpleNamespace(verification_state=state, action_type="payment_link",
                           amount_minor=2000)


@pytest.mark.parametrize("link,expected", [
    (None, (CandidateStatus.ATTEMPTED, 0)),                       # unreadable
    (SimpleNamespace(status="created", amount_minor=2000, amount_paid_minor=0),
     (CandidateStatus.ATTEMPTED, 0)),                             # sent, unpaid
    (SimpleNamespace(status="partially_paid", amount_minor=2000, amount_paid_minor=500),
     (CandidateStatus.ATTEMPTED, 250)),                           # a quarter paid
    (SimpleNamespace(status="paid", amount_minor=2000, amount_paid_minor=2000),
     (CandidateStatus.RECOVERED, 1000)),                          # captured
], ids=["unreadable", "created", "partially_paid", "paid"])
def test_a_link_is_recovery_only_to_the_extent_it_was_paid(link, expected):
    assert _settle_one(None, _cand(), _link_action(), link) == expected


def test_an_unknown_action_recovers_nothing():
    action = _link_action(VerificationState.UNKNOWN)
    assert _settle_one(None, _cand(), action, None) == (CandidateStatus.UNKNOWN, 0)


def test_a_partially_paid_link_is_a_live_link_not_a_failed_one():
    class A:
        def get_payment_link(self, _):
            return SimpleNamespace(status="partially_paid", amount_minor=2000,
                                   amount_paid_minor=500, short_url="u")
    vr = verify_payment_link(A(), link_id="plink_X", expected_amount_minor=2000)
    assert vr.state is VerificationState.SUCCESS
    assert vr.actual["amount_paid_minor"] == 500


# ============================================================ Action Center
def _settled(db, owner):
    task, _ = _proposal(db, owner)
    action = approve_and_execute(db, task.id, owner)["action"]
    db.commit()
    return action


def _placements(client, action_id):
    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    return [s for s in body["sections"] if s != "awaiting_approval"
            and action_id in [r["id"] for r in body[s]]]


def test_a_submitted_action_nobody_verified_is_not_hidden(db, owner, client):
    action = _settled(db, owner)
    db.execute(text("""UPDATE agent_actions SET status = 'SUBMITTED',
                       verification_state = NULL,
                       created_at = now() - interval '10 minutes'
                       WHERE id = :i"""), {"i": action.id})
    db.commit()
    assert _placements(client, action.id) == ["unknown"]


def test_a_failed_action_has_its_own_section(db, owner, client):
    action = _settled(db, owner)
    assert _placements(client, action.id) == ["recently_completed"]
    db.execute(text("UPDATE agent_actions SET verification_state = 'FAILED', "
                    "status = 'FAILED' WHERE id = :i"), {"i": action.id})
    db.commit()
    assert _placements(client, action.id) == ["failed"]


# =========================================================== Command Center
def test_the_command_center_reports_agent_and_provider_posture(db, owner, client):
    body = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    assert body["provider"]["adapter_mode"] == "mock"
    assert body["provider"]["live"] is False, "a mock provider must never read as live"
    assert body["agent"]["provider"] in ("deterministic", "anthropic")
    assert isinstance(body["agent"]["runs_by_mode"], dict)
    rev = body["revenue"]
    assert rev["recovered_captured_minor"] + rev["recovered_refunded_minor"] \
        <= rev["recovered_minor"]


# ================================================================ evidence
def test_a_computed_figure_is_derived_not_observed(db, owner):
    out = AgentRuntime(db, owner, provider=DeterministicProvider()).run(
        "Why did revenue drop last week?")
    kinds = {f["metric"]: f["kind"] for f in out.task.findings if f.get("metric")}
    assert kinds.get("change_pct") == "DERIVED"
    assert kinds.get("current_period_revenue") == "OBSERVED"


def test_what_was_done_is_executed_and_what_was_confirmed_is_verified(db, owner):
    task, _ = _proposal(db, owner)
    r = approve_and_execute(db, task.id, owner)
    ev = {e.key: e.kind for e in r["result"].evidence}
    assert ev["external_reference"] == "EXECUTED"
    assert ev["verification_state"] == "VERIFIED"


def test_an_unsettled_read_is_never_labelled_verified(db, owner):
    task, _ = _proposal(db, owner)
    r = approve_and_execute(db, task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    assert r["action"].verification_state is VerificationState.UNKNOWN
    ev = {e.key: e.kind for e in r["result"].evidence}
    assert ev["verification_state"] == "OBSERVED"


# ====================================================== review fixes, pinned
def test_revoking_needs_the_permission_approving_would(db, owner, analyst):
    task, ap = _proposal(db, owner)
    with pytest.raises(ApprovalError) as e:
        revoke(db, task.id, analyst)
    assert e.value.code == "AUTHORIZATION_DENIED"
    db.refresh(ap)
    assert ap.decision == "PENDING"


def test_money_paid_on_a_link_that_then_expired_stays_recovered():
    expired = SimpleNamespace(status="expired", amount_minor=2000, amount_paid_minor=500)
    assert _settle_one(None, _cand(), _link_action(VerificationState.FAILED), expired) \
        == (CandidateStatus.FAILED, 250)


def test_a_failed_read_never_erases_money_already_established():
    cand = _cand()
    cand.actual_recovery_minor = 250
    assert _settle_one(None, cand, _link_action(VerificationState.UNKNOWN), None) \
        == (CandidateStatus.UNKNOWN, 250)


def test_an_expired_key_is_reclaimed_without_expiring_again(db):
    first = claim(db, merchant_id="MERCH_A", operation="op.ttl", business_key="k-ttl",
                  request={"a": 1}, ttl_seconds=60)
    db.execute(text("UPDATE idempotency_records SET expires_at = now() - interval '1 minute' "
                    "WHERE id = :i"), {"i": first.record.id})
    db.expire_all()
    again = claim(db, merchant_id="MERCH_A", operation="op.ttl", business_key="k-ttl",
                  request={"a": 2})
    assert again.fresh and again.record.expires_at is None


def test_a_refund_that_loses_the_live_refund_race_settles_its_claim(db, owner):
    """The reservation lost, nothing was sent, and the idempotency record says
    so rather than sitting IN_PROGRESS forever."""
    from app.tools.actions import derive_idempotency_key

    task, ap = _proposal(db, owner)
    pid = ap.action_payload["synthetic_payment_id"]
    adapter = get_adapter(db)
    first = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                           synthetic_payment_id=pid, amount_minor=1000, approval_id=ap.id)
    assert first.result.success

    lost = execute_refund(db, adapter, task_id=task.id, merchant_id="MERCH_A",
                          synthetic_payment_id=pid, amount_minor=1000,
                          approval_id="APR_SOMEONE_ELSE")
    assert lost.action is None
    key = derive_idempotency_key("MERCH_A", first.action.external_payment_id, "refund",
                                 "APR_SOMEONE_ELSE")
    rec = db.query(IdempotencyRecord).filter(IdempotencyRecord.business_key == key).one()
    assert rec.status == "FAILED"


def test_a_part_paid_link_is_counted_once_in_the_ledger(db):
    """Its paid share is recovered and only the remainder is outstanding: the
    parts add up to what was attempted, and no rupee appears twice."""
    from app.detection import detect
    from app.models import Incident, IncidentType, RecoveryCandidate
    from app.recovery import plan_recovery
    from app.recovery.ledger import build_ledger

    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.PAYMENT_DEGRADATION).first()
    plan = plan_recovery(db, inc).plan
    cand = (db.query(RecoveryCandidate)
            .filter(RecoveryCandidate.plan_id == plan.id,
                    RecoveryCandidate.attributed_amount_minor > 3).first())
    assert cand is not None

    before = build_ledger(db, "MERCH_A")
    share = cand.attributed_amount_minor // 4
    cand.status = CandidateStatus.ATTEMPTED
    cand.actual_recovery_minor = share
    db.flush()
    after = build_ledger(db, "MERCH_A")

    assert after.recovered_minor - before.recovered_minor == share
    assert after.outstanding_minor - before.outstanding_minor \
        == cand.attributed_amount_minor - share
    assert after.attempted_minor - before.attempted_minor == cand.attributed_amount_minor
    assert after.invariants() == []
