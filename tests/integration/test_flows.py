"""End-to-end flow tests — the two acceptance tests in CONTRACT §58."""
from __future__ import annotations

from sqlalchemy import text

from app.agent.approval import approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.models import AgentAction, Refund, TaskStatus, VerificationState


# ------------------------------------------- CONTRACT §58 acceptance test 1
def test_agent_discovers_planted_revenue_cause_via_tools(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    assert out.status is TaskStatus.COMPLETED
    tools = [r[0] for r in db.execute(text(
        "SELECT tool_name FROM tool_calls WHERE task_id=:t ORDER BY seq"),
        {"t": out.task.id}).all()]
    assert "get_revenue_summary" in tools
    assert "get_payment_metrics" in tools
    # The planted cause must be DISCOVERED, never stated in the prompt.
    from app.agent.prompts.investigator_v1 import SYSTEM_PROMPT
    assert "upi" not in SYSTEM_PROMPT.lower()
    assert "upi" in out.answer.lower()


def test_every_observed_finding_is_grounded(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    valid = {r[0] for r in db.execute(text(
        "SELECT id FROM tool_calls WHERE task_id=:t"), {"t": out.task.id}).all()}
    from app.tools.contracts import Finding
    observed = [Finding(**f) for f in out.task.findings if f["kind"] == "OBSERVED"]
    assert observed
    assert all(f.is_grounded(valid) for f in observed)


# ------------------------------------------- CONTRACT §58 acceptance test 2
def test_duplicate_refund_full_loop(db, owner):
    before = db.query(Refund).count()
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")

    # policy stops execution and no external call happens yet
    assert out.status is TaskStatus.AWAITING_APPROVAL
    assert out.approval is not None
    assert db.query(AgentAction).filter(AgentAction.task_id == out.task.id).count() == 0
    assert db.query(Refund).count() == before

    r = approve_and_execute(db, out.task.id, owner)
    action = r["action"]
    assert action.verification_state is VerificationState.SUCCESS
    assert action.external_reference is not None
    assert action.external_payment_id.startswith("pay_")
    assert db.query(Refund).count() == before + 1
    assert r["task"].status is TaskStatus.COMPLETED


def test_rejection_makes_no_external_call(db, owner):
    before = db.query(Refund).count()
    out = AgentRuntime(db, owner).run("Refund the duplicate payment.")
    task = reject(db, out.task.id, owner)
    assert task.status is TaskStatus.REJECTED
    assert task.failure_code == "APPROVAL_REJECTED"
    assert db.query(Refund).count() == before
    assert db.query(AgentAction).filter(AgentAction.task_id == out.task.id).count() == 0


# --------------------------------------------------------------- UNKNOWN
def test_lost_response_yields_unknown_then_resolves(db, owner):
    """The case UNKNOWN exists for: the refund lands, the response is lost."""
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    action = r["action"]
    assert action.verification_state is VerificationState.UNKNOWN
    assert action.external_reference is None
    assert r["task"].failure_code == "EXTERNAL_STATE_UNKNOWN"

    rv = reverify(db, out.task.id, owner)
    assert rv["verification"].state is VerificationState.SUCCESS
    assert rv["action"].external_reference is not None
    n = db.execute(text(
        "SELECT count(*) FROM refunds WHERE payment_id='SYN_PAY_0002'")).scalar()
    assert n == 1, "reconciliation created a second refund"


def test_provider_error_is_not_swallowed(db, owner):
    before = db.query(Refund).count()
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.PROVIDER_5XX))
    assert r["task"].status is TaskStatus.FAILED
    assert db.query(Refund).count() == before


# ---------------------------------------------------------------- replay
def test_playback_executes_nothing(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    pb = playback(db, out.task.id)
    assert pb["external_calls_made"] == 0
    assert [s["tool"] for s in pb["steps"]]


def test_re_reason_makes_no_financial_side_effect(db, owner):
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    approve_and_execute(db, out.task.id, owner)
    before = db.query(Refund).count()
    rr = re_reason(db, out.task.id, owner)
    assert rr["external_calls_made"] == 0
    assert db.query(Refund).count() == before
    assert rr["reasoning_diverged"] is False


def test_re_reason_read_only_task_is_consistent(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    rr = re_reason(db, out.task.id, owner)
    assert rr["reasoning_diverged"] is False
    assert rr["policy_diverged"] is False


# ----------------------------------------------------------------- budget
def test_budget_terminates_runaway_loop(db, owner, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "max_tool_calls_per_task", 1)
    monkeypatch.setattr(s, "max_llm_turns_per_task", 8)
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    assert out.status is TaskStatus.ABORTED_BUDGET
    assert out.task.failure_code == "BUDGET_EXCEEDED"


# ------------------------------------------------------------------ audit
def test_trace_records_the_whole_loop(db, owner):
    from app.audit.trace import trace_for
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    approve_and_execute(db, out.task.id, owner)
    events = [e["event"] for e in trace_for(db, out.task.id)]
    for expected in ("task_created", "llm_turn", "policy_decision",
                     "approval_requested", "action_executing",
                     "action_recorded", "verification", "task_completed"):
        assert expected in events, f"missing audit event: {expected}"

# ---------------------------------------------------- idempotency reserve path
def _approved_refund_args(db, owner):
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    approval = out.approval
    assert approval is not None
    approval.decision = "APPROVED"
    db.flush()
    return out, approval, approval.action_payload


def test_second_execute_is_refused_by_the_balance_check(db, owner):
    """In the ordinary path the refundable-balance precondition fires first.

    After a successful refund the payment has no refundable balance left, so a
    repeat is rejected before the idempotency key is ever consulted. That is
    correct defence in depth — and it is why the UNIQUE constraint needs its own
    test (below) rather than being assumed covered by this one.
    """
    from app.integrations.razorpay.adapter import get_adapter
    from app.tools.actions import execute_refund

    out, approval, payload = _approved_refund_args(db, owner)
    adapter = get_adapter(db)
    before = db.query(Refund).count()

    kw = dict(task_id=out.task.id, merchant_id=owner.merchant_id,
              synthetic_payment_id=payload["synthetic_payment_id"],
              amount_minor=int(payload["amount_minor"]), approval_id=approval.id)

    first = execute_refund(db, adapter, **kw)
    assert first.action is not None
    assert db.query(Refund).count() == before + 1

    second = execute_refund(db, adapter, **kw)
    assert second.action is None
    assert second.result.error_code == "TOOL_INVALID_ARGUMENT"
    assert second.result.data["error"] == "exceeds_refundable_balance"
    assert db.query(Refund).count() == before + 1


def test_unique_idempotency_key_blocks_the_retry_the_balance_check_cannot(db, owner):
    """CONTRACT §24 — the reserve-before-call branch.

    Reaching it requires a first attempt that leaves the refundable balance
    untouched, which ACCEPTED_NOT_APPLIED produces: the provider issues a refund
    id but the payment never moves. The balance check therefore passes on the
    retry, and the UNIQUE constraint on idempotency_key is the only thing
    standing between the caller and a second provider call.
    """
    from app.integrations.razorpay.adapter import get_adapter
    from app.tools.actions import execute_refund

    out, approval, payload = _approved_refund_args(db, owner)
    kw = dict(task_id=out.task.id, merchant_id=owner.merchant_id,
              synthetic_payment_id=payload["synthetic_payment_id"],
              amount_minor=int(payload["amount_minor"]), approval_id=approval.id)

    stalled = get_adapter(db, FaultInjector(fault=Fault.ACCEPTED_NOT_APPLIED))
    first = execute_refund(db, stalled, **kw)
    assert first.action is not None

    # Balance is untouched, so the retry gets past the precondition...
    second = execute_refund(db, get_adapter(db), **kw)

    # ...and is stopped by the reserved row instead.
    assert second.action is None
    assert second.result.error_code == "PARTIAL_EXECUTION"
    assert second.result.data["error"] == "duplicate_action"
    assert db.query(AgentAction).filter(
        AgentAction.task_id == out.task.id).count() == 1


def test_a_random_key_would_let_that_retry_through(db, owner, monkeypatch):
    """Proves the test above measures the KEY, not some other guard.

    With derivation replaced by a random value the reserved row never collides,
    the retry reaches the provider, and a second refund lands.
    """
    import uuid as _uuid

    from app.integrations.razorpay.adapter import get_adapter
    from app.tools import actions as actions_mod

    out, approval, payload = _approved_refund_args(db, owner)
    kw = dict(task_id=out.task.id, merchant_id=owner.merchant_id,
              synthetic_payment_id=payload["synthetic_payment_id"],
              amount_minor=int(payload["amount_minor"]), approval_id=approval.id)

    stalled = get_adapter(db, FaultInjector(fault=Fault.ACCEPTED_NOT_APPLIED))
    actions_mod.execute_refund(db, stalled, **kw)

    monkeypatch.setattr(actions_mod, "derive_idempotency_key",
                        lambda *a, **k: _uuid.uuid4().hex)
    before = db.query(Refund).count()
    second = actions_mod.execute_refund(db, get_adapter(db), **kw)

    assert second.action is not None, "a random key let the retry reserve a new action"
    assert db.query(Refund).count() == before + 1, "and reach the provider"
