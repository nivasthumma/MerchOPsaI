"""The runtime's first two gates — CONTRACT §10, §13, §33.

A full mutation run found both of these ungraded: breaking either one crashed
the scenario suite and every unit test passed. They had been reading as CAUGHT
since before the crash-scoring fix, on the strength of the suite falling over
rather than anything detecting them.

The gap was subtle. `tests/security/test_security.py` has
`test_model_cannot_call_unregistered_tool`, which asserts the POLICY ENGINE
denies `exec_shell` — a different control at a different layer. The runtime's
registry lookup runs *before* policy is consulted and returns
`TOOL_UNAVAILABLE`, and nothing exercised it.

Both gates are tested here through a real run rather than by calling
`_handle_tool` directly, because what matters is that the rejection is
**recorded and graceful**: a ToolCall row with the right error code, the loop
continuing, and no execution. A gate that raises instead of refusing is not the
same control — it is a crash that happens to prevent the same thing.
"""
from __future__ import annotations

import pytest

from app.agent.runtime import AgentRuntime
from app.llm.deterministic import DeterministicProvider
from app.models import AgentAction, ToolCall


class _RequestsUnregisteredTool(DeterministicProvider):
    """Renames whatever the planner asked for to a tool that does not exist.

    The deterministic planner can only emit registered tools, so gate 1 is
    unreachable without this — which is why it had no test.
    """

    def turn(self, **kw):
        t = super().turn(**kw)
        for r in t.tool_requests:
            r.name = "exec_shell"
            r.arguments = {"cmd": "rm -rf /"}
        return t


class _MalformsArguments(DeterministicProvider):
    """Keeps a real tool name and sends arguments of the wrong type."""

    def turn(self, **kw):
        t = super().turn(**kw)
        for r in t.tool_requests:
            r.name = "get_payment"
            r.arguments = {"payment_id": 12345}     # int where a string is required
        return t


def _calls(db, task_id: str) -> list[ToolCall]:
    return (db.query(ToolCall).filter(ToolCall.task_id == task_id)
            .order_by(ToolCall.seq).all())


# ------------------------------------------------------- gate 1: the registry
def test_an_unregistered_tool_is_refused_and_recorded(db, owner):
    """CONTRACT §10. Refused, not crashed.

    Breaking this gate leaves `spec` as None and the next line raises, which
    stops the run rather than rejecting the call — indistinguishable from a
    control that works, and invisible to a suite that only checks the run died.
    """
    out = AgentRuntime(db, owner, provider=_RequestsUnregisteredTool()).run(
        "Why did revenue drop this week?")

    rejected = [c for c in _calls(db, out.task.id) if c.tool_name == "exec_shell"]
    assert rejected, "the unregistered tool call was never recorded"
    for c in rejected:
        assert c.success is False
        assert c.error_code == "TOOL_UNAVAILABLE"
        assert "not registered" in c.output["error"]

    # The run finished on its own terms rather than dying.
    assert out.task.status is not None
    assert out.task.final_answer is not None


def test_an_unregistered_tool_reaches_neither_policy_nor_the_provider(db, owner):
    """The registry gate runs BEFORE policy, so a refusal here should leave no
    policy decision and no action behind it."""
    out = AgentRuntime(db, owner, provider=_RequestsUnregisteredTool()).run(
        "Why did revenue drop this week?")

    rejected = [c for c in _calls(db, out.task.id) if c.tool_name == "exec_shell"]
    assert rejected
    assert all(c.policy_decision is None for c in rejected), (
        "an unregistered tool was evaluated by policy")
    assert db.query(AgentAction).filter(
        AgentAction.task_id == out.task.id).count() == 0


def test_the_rejection_is_audited(db, owner):
    from sqlalchemy import text

    out = AgentRuntime(db, owner, provider=_RequestsUnregisteredTool()).run(
        "Why did revenue drop this week?")
    events = [r[0] for r in db.execute(text(
        "SELECT event_type FROM audit_logs WHERE task_id = :t"),
        {"t": out.task.id}).all()]
    assert "tool_rejected" in events


# --------------------------------------------- gate 2: argument validation
def test_malformed_arguments_are_refused_before_anything_reads_them(db, owner):
    """CONTRACT §13, §33. Validation MUST precede policy.

    The policy engine queries the database with these values, so an unvalidated
    model-supplied value is both a crash and an injection surface. Skipping the
    gate passes the bad value straight through — which raises somewhere further
    down instead of being refused here.
    """
    out = AgentRuntime(db, owner, provider=_MalformsArguments()).run(
        "Look up a payment.")

    bad = [c for c in _calls(db, out.task.id) if c.tool_name == "get_payment"]
    assert bad, "the malformed call was never recorded"
    for c in bad:
        assert c.success is False
        assert c.error_code == "TOOL_INVALID_ARGUMENT"
        # The risk class is recorded even on a refusal: a rejected call is
        # still a call somebody tried to make.
        assert c.risk_level is not None


def test_a_refused_call_does_not_stop_the_run(db, owner):
    """Both gates refuse and return; neither aborts. A run that died on a bad
    tool call would lose the trace of everything before it."""
    out = AgentRuntime(db, owner, provider=_MalformsArguments()).run(
        "Look up a payment.")
    assert out.task.final_answer is not None
    assert db.query(AgentAction).filter(
        AgentAction.task_id == out.task.id).count() == 0
