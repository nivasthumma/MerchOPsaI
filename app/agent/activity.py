"""Agent activity, as operational progress — plan P0-08.

    ✓ Investigation started
    ✓ Revenue summary
    ✓ Payment metrics
    ✓ Failure breakdown
    ✓ Evidence corroborated
    ✓ Revenue impact calculated
    ✓ Recovery candidates calculated
    ✓ Policy evaluated
    ⏸ Waiting for approval

The plan's reason for this is exact, and it is the whole design constraint:

> This demonstrates genuine AI without exposing private reasoning.

So every step here is derived from something the application **recorded doing** —
a `tool_calls` row, a `policy_decision` on one of them, an `approvals` row, an
`agent_actions` row and its verification state. Nothing reads `final_answer`,
`findings`, or any other model-authored text.

That distinction is not stylistic. A progress list assembled from prose is a
list the model can write whatever it likes into, including steps that never
happened; §28 of the plan forbids exposing chain-of-thought and this is the
constructive form of the same rule. A step appears here because a row exists.

## Why it is computed server-side

The browser has the tool calls and could label them itself. It would then hold
a second copy of the tool→label mapping, which is exactly the drift the label
was put on `ToolSpec` to avoid — and a client that classified a step as `done`
would be a client with an opinion about whether a financial action completed.
"""
from __future__ import annotations

from app.models import ActionStatus, AgentTask, Approval, TaskStatus, VerificationState

# What a step is claiming.
DONE = "done"          # it happened, and it worked
FAILED = "failed"      # it happened, and it did not
BLOCKED = "blocked"    # it is waiting on a person
RUNNING = "running"    # in flight right now
PENDING = "pending"    # expected, not reached


def _step(key: str, label: str, state: str, at=None, detail: str = "") -> dict:
    return {"key": key, "label": label, "state": state,
            "at": at.isoformat() if at is not None else None,
            "detail": detail}


def build(session, task: AgentTask) -> list[dict]:
    """The activity list for one task, oldest first.

    Ordered by what actually happened rather than by a fixed template: a task
    that never reached policy has no policy step, and inventing a greyed-out
    one would imply a stage this run was going to have.
    """
    from app.models import AgentAction, ToolCall
    from app.tools.registry import REGISTRY

    steps: list[dict] = [
        _step("started", "Investigation started", DONE, task.created_at,
              task.request[:160]),
    ]

    calls = (session.query(ToolCall)
             .filter(ToolCall.task_id == task.id)
             .order_by(ToolCall.seq).all())

    # --- one step per tool call, labelled by its own spec ------------------
    sources: set[str] = set()
    policy_seen: str | None = None
    for c in calls:
        spec = REGISTRY.get(c.tool_name)
        # A call to a tool the registry does not know cannot happen — the
        # runtime refuses it — but if it ever did, naming it is better than
        # dropping it from the operator's view of what ran.
        label = (spec.activity_label if spec and spec.activity_label
                 else f"Ran {c.tool_name}")
        steps.append(_step(
            f"tool:{c.seq}", label,
            DONE if c.success else FAILED,
            c.created_at,
            "" if c.success else (c.error_code or "failed"),
        ))
        if c.success:
            sources.add(c.tool_name)
        if c.policy_decision and policy_seen is None:
            policy_seen = c.policy_decision

    # --- corroboration, as a count of distinct successful reads -----------
    #
    # Not a claim that the evidence AGREES — nothing here can establish that,
    # and saying "corroborated" about a single source would be the overclaim
    # the plan's evidence graph exists to prevent. It reports how many
    # independent reads the conclusions rest on, and says when that is one.
    if len(sources) > 1:
        steps.append(_step(
            "corroboration", "Evidence corroborated", DONE, None,
            f"{len(sources)} independent reads"))
    elif len(sources) == 1:
        steps.append(_step(
            "corroboration", "Single source", FAILED, None,
            "One read. Nothing corroborates it."))

    # --- impact, only when the calculation engine actually ran ------------
    if task.recommendation:
        steps.append(_step("impact", "Revenue impact calculated", DONE, None,
                           "Computed by the control plane, not the model."))

    # --- policy ------------------------------------------------------------
    if policy_seen:
        steps.append(_step("policy", "Policy evaluated", DONE, None, policy_seen))

    # --- the human gate ----------------------------------------------------
    approvals = (session.query(Approval)
                 .filter(Approval.task_id == task.id)
                 .order_by(Approval.created_at).all())
    for a in approvals:
        if a.decision == "PENDING":
            signed = len([s for s in a.signatures if s.decision == "APPROVED"])
            steps.append(_step(
                f"approval:{a.id}", "Waiting for approval", BLOCKED, a.created_at,
                f"{signed} of {a.required_signatures} signature(s). Nothing has "
                f"reached the provider."))
        elif a.decision == "APPROVED":
            steps.append(_step(f"approval:{a.id}", "Approved", DONE, a.decided_at,
                               f"by {a.decided_by}"))
        else:
            steps.append(_step(
                f"approval:{a.id}", f"{a.decision.title()}", FAILED, a.decided_at,
                "No provider call was made."))

    # --- execution and verification, per action ---------------------------
    actions = (session.query(AgentAction)
               .filter(AgentAction.task_id == task.id)
               .order_by(AgentAction.created_at).all())
    for a in actions:
        steps.append(_step(
            f"exec:{a.id}", "Sent to provider",
            FAILED if a.status is ActionStatus.FAILED else DONE,
            a.created_at,
            a.external_reference or "no reference issued"))

        if a.verification_state is None:
            steps.append(_step(f"verify:{a.id}", "Verifying", RUNNING, None,
                               "Reading provider state back."))
            continue
        if a.verification_state is VerificationState.SUCCESS:
            steps.append(_step(f"verify:{a.id}", "Independently verified", DONE,
                               a.last_verified_at, "The money moved."))
        elif a.verification_state is VerificationState.FAILED:
            steps.append(_step(f"verify:{a.id}", "Verified as not taken effect",
                               FAILED, a.last_verified_at, "No money moved."))
        else:
            steps.append(_step(
                f"verify:{a.id}", "Outcome unknown", BLOCKED, a.last_verified_at,
                f"Attempt {a.verify_attempts}. "
                + ("A person owns this now." if a.escalated
                   else "Being reconciled automatically.")))

    # --- how the run ended, when it ended badly ---------------------------
    #
    # A run that stopped because it hit a bound must say so. Left off, an
    # ABORTED_BUDGET task looks like one that simply had less to do.
    if task.status is TaskStatus.ABORTED_BUDGET:
        steps.append(_step("budget", "Stopped — budget exceeded", FAILED, None,
                           "The run exceeded its bounds and was stopped."))
    elif task.status is TaskStatus.DENIED:
        steps.append(_step("denied", "Denied by policy", FAILED, None,
                           "Refused before any provider call."))

    return steps
