"""Agent activity as operational progress — plan P0-08.

The property that matters is not that the list is pretty. It is that every step
corresponds to something the application recorded doing, so a model cannot put
a step into it that never happened. Most of these tests are that claim.
"""
from __future__ import annotations

from sqlalchemy import text

from app.agent.activity import build as agent_activity
from app.agent.runtime import AgentRuntime


def _labels(steps) -> list[str]:
    return [s["label"] for s in steps]


def _by_key(steps, prefix: str) -> list[dict]:
    return [s for s in steps if s["key"].startswith(prefix)]


def test_every_step_is_backed_by_a_recorded_row(db, owner):
    """The whole design constraint. A step exists because a row exists."""
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()

    steps = agent_activity(db, out.task)
    tool_steps = _by_key(steps, "tool:")

    recorded = db.execute(text(
        "SELECT COUNT(*) FROM tool_calls WHERE task_id = :t"),
        {"t": out.task.id}).scalar()
    assert len(tool_steps) == recorded
    assert recorded > 0, "the fixture must actually call tools"


def test_the_list_does_not_read_model_prose(db, owner):
    """§28 forbids exposing chain-of-thought; this is the constructive form of
    the same rule. Rewriting everything the model authored must not change a
    single step."""
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()
    before = agent_activity(db, out.task)

    out.task.final_answer = "I decided to refund everything and I already did."
    out.task.findings = [{"claim": "a step that never happened",
                          "kind": "OBSERVED", "evidence_refs": []}]
    db.flush()

    assert agent_activity(db, out.task) == before


def test_a_tool_is_named_by_its_own_spec(db, owner):
    """The label lives on `ToolSpec`. A second mapping in this module would be
    a second list to keep in step, and the failure mode of forgetting it is a
    screen that reads `get_failure_breakdown` at the merchant."""
    from app.tools.registry import REGISTRY

    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()
    steps = agent_activity(db, out.task)

    called = db.execute(text(
        "SELECT tool_name FROM tool_calls WHERE task_id = :t ORDER BY seq"),
        {"t": out.task.id}).scalars().all()
    expected = [REGISTRY[n].activity_label for n in called]
    assert [s["label"] for s in _by_key(steps, "tool:")] == expected
    # And none of them leaks the underlying identifier into the label.
    assert not any("_" in s["label"] for s in _by_key(steps, "tool:"))


def test_every_registered_tool_has_a_label(db):
    """A tool without one renders as `Ran get_whatever`, which is the fallback
    working rather than the intent. The registry is small enough to require
    them all."""
    from app.tools.registry import REGISTRY

    missing = [n for n, s in REGISTRY.items() if not s.activity_label]
    assert missing == []


def test_a_failed_tool_call_is_not_reported_as_done(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()
    first_seq = db.execute(text(
        "SELECT MIN(seq) FROM tool_calls WHERE task_id = :t"),
        {"t": out.task.id}).scalar()
    db.execute(text("UPDATE tool_calls SET success = false, "
                    "error_code = 'TOOL_TIMEOUT' WHERE task_id = :t AND seq = :s"),
               {"t": out.task.id, "s": first_seq})
    # The runtime already loaded these rows, so the session would serve the
    # pre-UPDATE objects from its identity map and the assertion below would
    # be testing the fixture rather than the builder.
    db.expire_all()

    first = _by_key(agent_activity(db, out.task), "tool:")[0]
    assert first["state"] == "failed"
    assert first["detail"] == "TOOL_TIMEOUT"


def test_a_single_source_is_stated_as_uncorroborated(db, owner):
    """"Corroborated" over one read is the overclaim the evidence graph exists
    to prevent."""
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()
    # Collapse every call onto one tool: one distinct successful source.
    db.execute(text("UPDATE tool_calls SET tool_name = 'get_revenue_summary' "
                    "WHERE task_id = :t"), {"t": out.task.id})
    db.expire_all()

    step = next(s for s in agent_activity(db, out.task)
                if s["key"] == "corroboration")
    assert step["label"] == "Single source"
    assert step["state"] == "failed"
    assert "Nothing corroborates it" in step["detail"]


def test_several_sources_are_counted_not_asserted_to_agree(db, owner):
    """It reports how many independent reads the conclusions rest on. Nothing
    here can establish that they AGREE, and it does not say so."""
    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()

    step = next(s for s in agent_activity(db, out.task)
                if s["key"] == "corroboration")
    assert step["label"] == "Evidence corroborated"
    assert "independent reads" in step["detail"]


def test_a_pending_approval_blocks_and_says_nothing_reached_the_provider(db, owner):
    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    assert out.approval is not None
    db.flush()

    steps = agent_activity(db, out.task)
    gate = next(s for s in steps if s["key"].startswith("approval:"))
    assert gate["label"] == "Waiting for approval"
    assert gate["state"] == "blocked"
    assert "Nothing has reached the provider" in gate["detail"]
    # And nothing claims execution happened.
    assert _by_key(steps, "exec:") == []


def test_the_whole_path_appears_once_it_has_run(db, owner):
    from app.agent.approval import approve_and_execute

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    approve_and_execute(db, out.task.id, owner)
    db.flush()

    labels = _labels(agent_activity(db, out.task))
    for expected in ("Investigation started", "Policy evaluated", "Approved",
                     "Sent to provider", "Independently verified"):
        assert expected in labels, (expected, labels)


def test_an_unknown_outcome_is_blocked_and_says_who_owns_it(db, owner):
    from app.agent.approval import approve_and_execute
    from app.integrations.razorpay.faults import Fault, FaultInjector

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    db.flush()

    step = next(s for s in agent_activity(db, out.task)
                if s["key"].startswith("verify:"))
    assert step["label"] == "Outcome unknown"
    assert step["state"] == "blocked"
    assert "Being reconciled automatically" in step["detail"]

    # And once a human owns it, the step says that instead.
    from app.verification.schedule import escalate
    escalate(db, r["action"], reason="test")
    db.flush()
    step = next(s for s in agent_activity(db, out.task)
                if s["key"].startswith("verify:"))
    assert "A person owns this now" in step["detail"]


def test_a_rejection_records_that_no_provider_call_was_made(db, owner):
    from app.agent.approval import reject

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    reject(db, out.task.id, owner, reason="not this one")
    db.flush()

    steps = agent_activity(db, out.task)
    gate = next(s for s in steps if s["key"].startswith("approval:"))
    assert gate["state"] == "failed"
    assert "No provider call was made" in gate["detail"]
    assert _by_key(steps, "exec:") == []


def test_a_run_stopped_by_its_budget_says_so(db, owner):
    """Left off, an ABORTED_BUDGET task looks like one that had less to do."""
    from app.models import TaskStatus

    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    out.task.status = TaskStatus.ABORTED_BUDGET
    db.flush()

    steps = agent_activity(db, out.task)
    assert steps[-1]["label"] == "Stopped — budget exceeded"
    assert steps[-1]["state"] == "failed"


def test_the_api_serves_the_activity(db, owner):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    out = AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        body = c.get(f"/tasks/{out.task.id}",
                     headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}
                     ).json()
    sec.reset_rate_limits()

    assert body["activity"][0]["label"] == "Investigation started"
    assert all(s["state"] in ("done", "failed", "blocked", "running", "pending")
               for s in body["activity"])
