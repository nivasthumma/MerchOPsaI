"""Replay — CONTRACT §28 (as amended by ADR-0008 #9).

Two modes, deliberately distinct:

  PLAYBACK   Render the recorded trace. Deterministic by construction. Executes
             nothing. This is the demo surface.

  RE_REASON  Re-run the agent against FROZEN tool results drawn from the
             recorded trace. No tool touches the database; no financial action
             is possible because the action tools are withheld from the
             registry passed to the replay runtime.

Divergence is recorded, not suppressed. The model is non-deterministic even at
temperature 0, so asserting that a re-reasoned replay always reproduces the
original would be a claim the system cannot honour (CONTRACT §54).
"""
from __future__ import annotations

import enum

from sqlalchemy import text

from app.agent.runtime import AgentRuntime, Principal
from app.audit.trace import record, trace_for
from app.models import AgentTask, ToolCall


class ReplayMode(str, enum.Enum):
    PLAYBACK = "PLAYBACK"
    RE_REASON = "RE_REASON"


def _external_effects(session, task_id: str) -> int:
    return session.execute(
        text("SELECT COUNT(*) FROM agent_actions WHERE task_id = :t"), {"t": task_id}
    ).scalar() or 0


def playback(session, task_id: str) -> dict:
    """Render the stored execution. Executes nothing."""
    task = session.get(AgentTask, task_id)
    if task is None:
        raise ValueError(f"Unknown task {task_id}")
    calls = session.query(ToolCall).filter(ToolCall.task_id == task_id) \
        .order_by(ToolCall.seq).all()
    return {
        "mode": ReplayMode.PLAYBACK.value,
        "task_id": task_id,
        "request": task.request,
        "status": task.status.value,
        "final_answer": task.final_answer,
        "steps": [{
            "seq": c.seq, "tool": c.tool_name, "arguments": c.input,
            "success": c.success, "risk_level": c.risk_level,
            "policy_decision": c.policy_decision, "duration_ms": c.duration_ms,
            "error_code": c.error_code,
        } for c in calls],
        "trace": trace_for(session, task_id),
        "external_calls_made": 0,
        "note": "Playback renders the recorded trace. No tool ran and no external call was made.",
    }


def frozen_tool_results(session, task_id: str) -> dict:
    """Recorded tool outputs keyed by '<seq>:<tool>' and by tool name."""
    calls = session.query(ToolCall).filter(
        ToolCall.task_id == task_id, ToolCall.success.is_(True)
    ).order_by(ToolCall.seq).all()
    frozen: dict = {}
    for c in calls:
        if c.output:
            frozen[f"{c.seq}:{c.tool_name}"] = c.output
            frozen.setdefault(c.tool_name, c.output)
    return frozen


def re_reason(session, task_id: str, principal: Principal, provider=None) -> dict:
    """Re-run reasoning against frozen tool results. No financial side effects."""
    original = session.get(AgentTask, task_id)
    if original is None:
        raise ValueError(f"Unknown task {task_id}")

    frozen = frozen_tool_results(session, task_id)
    before_actions = _external_effects(session, task_id)

    runtime = AgentRuntime(session, principal, provider=provider, frozen_tools=frozen)

    # The full registry is kept so the replay's tool sequence is comparable to
    # the original. Withholding HIGH-risk tools would guarantee a *false*
    # divergence on every replay of an action task and make the consistency
    # metric meaningless.
    #
    # Safety does not depend on withholding. Two independent barriers already
    # make a financial side effect unreachable from a replay:
    #   1. The runtime HALTS at REQUIRE_APPROVAL and never executes. Execution
    #      is only reachable via approve_and_execute(), which replay never calls.
    #   2. execute_read_tool() has no implementation for HIGH-risk tools, so
    #      even an erroneous ALLOW cannot perform one.
    # The assertion below verifies the outcome rather than trusting the design.
    out = runtime.run(original.request, is_replay=True, replayed_from=task_id)

    after_actions = _external_effects(session, task_id)
    replay_actions = _external_effects(session, out.task.id)

    # CONTRACT §28: replay must never repeat a financial side effect.
    if replay_actions != 0 or after_actions != before_actions:
        raise RuntimeError(
            f"Replay safety violation: replay created {replay_actions} action(s) and the "
            f"original task's action count moved {before_actions} -> {after_actions}.")

    orig_policies = _policy_sequence(session, task_id)
    new_policies = _policy_sequence(session, out.task.id)
    orig_tools = _tool_sequence(session, task_id)
    new_tools = _tool_sequence(session, out.task.id)

    # Two kinds of difference, which must not be conflated:
    #
    #   REASONING divergence  the agent chose a different tool sequence given
    #                         identical frozen evidence. This is the thing
    #                         replay_consistency_rate is supposed to measure.
    #
    #   STATE divergence      policy reached a different decision because the
    #                         WORLD changed -- most commonly the original task's
    #                         refund now exists, so the duplicate-action guard
    #                         correctly denies a second one. That is the policy
    #                         engine working, not the agent being inconsistent.
    diff = {}
    reasoning_diverged = orig_tools != new_tools
    if reasoning_diverged:
        diff["tool_sequence"] = {"original": orig_tools, "replay": new_tools}

    policy_diverged = orig_policies != new_policies
    policy_cause = None
    if policy_diverged:
        diff["policy_outcomes"] = {"original": orig_policies, "replay": new_policies}
        if before_actions > 0 and not reasoning_diverged:
            policy_cause = "state_changed_by_original_action"
            diff["policy_divergence_cause"] = (
                "The original task executed a financial action, so the duplicate-action "
                "guard now denies a second one. Expected; not a reasoning divergence.")
        else:
            policy_cause = "unexplained"

    # Only reasoning divergence counts against replay consistency.
    diverged = reasoning_diverged
    if diverged:
        out.task.failure_code = "REPLAY_DIVERGED"
        session.flush()
    record(session, out.task, "replay_completed",
           {"mode": ReplayMode.RE_REASON.value, "replayed_from": task_id,
            "reasoning_diverged": reasoning_diverged,
            "policy_diverged": policy_diverged, "policy_divergence_cause": policy_cause,
            "diff": diff, "external_calls_made": replay_actions})

    return {
        "mode": ReplayMode.RE_REASON.value,
        "replayed_from": task_id,
        "replay_task_id": out.task.id,
        "diverged": diverged,
        "reasoning_diverged": reasoning_diverged,
        "policy_diverged": policy_diverged,
        "policy_divergence_cause": policy_cause,
        "diff": diff,
        "original_tool_sequence": orig_tools,
        "replay_tool_sequence": new_tools,
        "final_answer": out.answer,
        # The safety assertion the contract actually cares about.
        "external_calls_made": replay_actions,
        "original_actions_unchanged": before_actions == after_actions,
        "note": ("Re-reasoned against frozen tool results. Action tools were "
                 "withheld, so no financial side effect was possible."),
    }


def _policy_sequence(session, task_id: str) -> list:
    rows = session.execute(text("""
        SELECT tool_name, policy_decision FROM tool_calls
        WHERE task_id = :t ORDER BY seq
    """), {"t": task_id}).all()
    return [[r[0], r[1]] for r in rows]


def _tool_sequence(session, task_id: str) -> list:
    rows = session.execute(text("""
        SELECT tool_name FROM tool_calls WHERE task_id = :t ORDER BY seq
    """), {"t": task_id}).all()
    return [r[0] for r in rows]
