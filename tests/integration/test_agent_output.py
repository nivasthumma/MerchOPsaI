"""The agent's structured output — MerchantOps §36, §37, §38.

The interesting assertions are not that the schema parses. They are that the
model's own numbers cannot move a control, and that a claim with no evidence
behind it is rejected rather than displayed.
"""
from __future__ import annotations

import json

import pytest

from app.agent.output import AgentOutput, check_grounding, split_output
from app.agent.runtime import AgentRuntime, evidence_index
from app.llm.deterministic import DeterministicProvider
from app.models import TaskStatus


def _block(**overrides) -> str:
    body = {"intent": "revenue_investigation",
            "findings": [{"type": "root_cause", "claim": "UPI degraded",
                          "evidence_ids": ["E1"]}],
            "recommendation": {"type": "payment_link_recovery", "detail": "d"},
            "confidence": 0.8, "requires_human": False}
    body.update(overrides)
    return "Prose answer here.\n\n```json\n" + json.dumps(body) + "\n```"


class _Emits(DeterministicProvider):
    """A provider whose final turn emits exactly the given block."""

    def __init__(self, text):
        self._text = text

    def turn(self, **kw):
        t = super()._plan(**kw)
        if t.wants_tools:
            return t
        t.text = self._text
        return t


# ------------------------------------------------------------------ parsing
def test_prose_and_block_are_separated():
    prose, block = split_output(_block())
    assert prose == "Prose answer here."
    assert json.loads(block)["intent"] == "revenue_investigation"


def test_the_block_never_leaks_into_the_answer(db, owner):
    """A scenario asserting an answer does not contain "50000" would start
    failing on a recommendation's own figures if the two were joined."""
    out = AgentRuntime(db, owner, provider=_Emits(_block())).run("Why did revenue drop?")
    assert "```" not in (out.task.final_answer or "")
    assert "confidence" not in (out.task.final_answer or "")
    assert out.task.final_answer == "Prose answer here."


@pytest.mark.parametrize("bad", [
    {"confidence": 5},                      # out of range
    {"confidence": "high"},                 # wrong type
    {"intent": ""},                         # empty
    {"findings": [{"type": "wat", "claim": "x"}]},   # unknown finding type
])
def test_a_malformed_block_fails_the_task(db, owner, bad):
    out = AgentRuntime(db, owner, provider=_Emits(_block(**bad))).run("Why did revenue drop?")
    assert out.task.status is TaskStatus.FAILED
    assert out.task.failure_code == "MODEL_INVALID_OUTPUT"


def test_an_invented_field_is_rejected(db, owner):
    """A model inventing a key is a model whose output we have stopped
    understanding."""
    text = _block()
    body = json.loads(text.split("```json\n")[1].split("\n```")[0])
    body["authorized"] = True
    out = AgentRuntime(db, owner, provider=_Emits(
        "Prose.\n\n```json\n" + json.dumps(body) + "\n```")).run("Why did revenue drop?")
    assert out.task.failure_code == "MODEL_INVALID_OUTPUT"


def test_no_block_at_all_is_not_a_failure(db, owner):
    """A task that answered without proposing anything is a legitimate outcome,
    and the deterministic planner is not the only provider this runs on."""
    out = AgentRuntime(db, owner, provider=_Emits("Just prose, no block.")).run(
        "Why did revenue drop?")
    assert out.task.status is TaskStatus.COMPLETED
    assert out.task.failure_code is None


# --------------------------------------------------------------- grounding
def test_a_claim_citing_evidence_that_does_not_exist_is_rejected(db, owner):
    """§36. A well-formed claim about nothing is a different defect from
    malformed JSON, and it gets its own failure code."""
    out = AgentRuntime(db, owner, provider=_Emits(_block(
        findings=[{"type": "root_cause", "claim": "UPI degraded",
                   "evidence_ids": ["E999"]}]))).run("Why did revenue drop?")
    assert out.task.status is TaskStatus.FAILED
    assert out.task.failure_code == "AGENT_GROUNDING_FAILURE"


def test_a_claim_citing_nothing_at_all_is_rejected(db, owner):
    out = AgentRuntime(db, owner, provider=_Emits(_block(
        findings=[{"type": "inference", "claim": "Probably fraud",
                   "evidence_ids": []}]))).run("Why did revenue drop?")
    assert out.task.failure_code == "AGENT_GROUNDING_FAILURE"


def test_uncertainty_and_recommendation_need_no_evidence():
    """"I could not establish X" is precisely a claim with nothing behind it,
    and a recommendation follows from findings rather than from evidence."""
    out = AgentOutput.model_validate({
        "intent": "i", "confidence": 0.5, "requires_human": True,
        "findings": [{"type": "uncertainty", "claim": "Cannot tell", "evidence_ids": []},
                     {"type": "recommendation", "claim": "Escalate", "evidence_ids": []}]})
    assert check_grounding(out, set()) is None


def test_evidence_ids_resolve_to_the_tool_calls_that_produced_them(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    index = evidence_index(db, out.task.id)
    assert index and all(k.startswith("E") for k in index)
    model = [f for f in out.task.findings if f.get("source") == "model"]
    assert model, "the planner emitted no model findings"
    for f in model:
        assert f["evidence_refs"], "a model finding resolved to no tool call"
        assert all(ref.startswith("TC_") for ref in f["evidence_refs"])


# ------------------------------------------------- the model decides nothing
def test_requires_human_false_cannot_clear_a_pending_approval(db, owner):
    """The floor rule again (ADR-0019), on a different field. Model output
    travels through prompts carrying merchant free text; if a confident `false`
    could relax a control, an injection would only have to sound sure."""
    out = AgentRuntime(db, owner, provider=_Emits(_block(requires_human=False))).run(
        "Find the duplicate payment and refund it.")
    # The run halted for approval, so the block above was never reached — which
    # is itself the point: the model does not get the last word on this.
    assert out.task.status is TaskStatus.AWAITING_APPROVAL
    assert out.approval is not None
    assert out.approval.decision == "PENDING"


def test_requires_human_true_is_recorded(db, owner):
    out = AgentRuntime(db, owner, provider=_Emits(_block(requires_human=True))).run(
        "Why did revenue drop?")
    assert out.task.model_requires_human is True


def test_confidence_is_recorded_and_gates_nothing(db, owner):
    """A confidence of 0.0 must change no decision. If it did, the number would
    be a control the model owns."""
    low = AgentRuntime(db, owner, provider=_Emits(_block(confidence=0.0))).run(
        "Why did revenue drop?")
    high = AgentRuntime(db, owner, provider=_Emits(_block(confidence=1.0))).run(
        "Why did revenue drop?")
    assert low.task.agent_confidence == 0.0
    assert high.task.agent_confidence == 1.0
    assert low.task.status is high.task.status
    assert low.task.failure_code == high.task.failure_code
    assert len(low.task.findings) == len(high.task.findings)


def test_the_recommendation_field_is_finally_written(db, owner):
    """Declared since the first schema and never populated until now."""
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    assert out.task.recommendation is not None
    assert out.task.recommendation["type"]
    assert out.task.intent


def test_deterministic_findings_survive_alongside_model_ones(db, owner):
    """OBSERVED findings are what make the grounding rate computable without
    asking a second model to judge the first. They are not replaced."""
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    kinds = {(f["kind"], f.get("source", "deterministic")) for f in out.task.findings}
    assert ("OBSERVED", "deterministic") in kinds
    assert any(src == "model" for _, src in kinds)


def test_the_planner_confidence_is_computed_not_asserted(db, owner):
    """A planner that hard-coded 0.9 would teach the suite to accept a number
    nothing produced."""
    thin = AgentRuntime(db, owner).run("Show me order SYN_ORD_DUP01.")
    rich = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    assert thin.task.agent_confidence is not None
    assert rich.task.agent_confidence is not None
    assert rich.task.agent_confidence >= thin.task.agent_confidence
    assert rich.task.agent_confidence < 1.0


# ------------------------------------------------- gaps found by mutation
def test_the_api_reports_a_human_is_required_whatever_the_model_said(db, owner):
    """Found by a surviving mutant. The test above asserts the task halts; it
    never asserted what the API tells a client, so replacing the OR with the
    model's own field changed nothing that was checked.

    A pending approval means a human is required. The model saying otherwise is
    recorded and ignored.
    """
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    out = AgentRuntime(db, owner, provider=_Emits(_block(requires_human=False))).run(
        "Find the duplicate payment and refund it.")
    assert out.approval is not None
    db.commit()

    sec.reset_rate_limits()
    with TestClient(app) as c:
        view = c.get(f"/tasks/{out.task.id}",
                     headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}).json()
    sec.reset_rate_limits()

    assert view["requires_human"] is True
    assert view["model_requires_human"] is False


def test_evidence_labels_are_unique_across_a_whole_task(db, owner):
    """Also found by a surviving mutant. The unit test drove the renderer
    directly with explicit start ids, so breaking the CALLER — passing 0 every
    time — was invisible.

    If numbering restarts per tool call, `E1` names a different value in every
    result. A model citing E1 still resolves, to the wrong thing, and grounding
    cannot tell.
    """
    import re

    seen: list[list[str]] = []

    class Recording(DeterministicProvider):
        def turn(self, **kw):
            for m in kw["messages"]:
                if m["role"] == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            labels = re.findall(r"\bE\d+\b", str(b.get("content", "")))
                            if labels:
                                seen.append(labels)
            return super().turn(**kw)

    out = AgentRuntime(db, owner, provider=Recording()).run("Why did revenue drop this week?")
    assert out.task.tool_call_count >= 2

    # The same result is re-shown on each turn, so dedupe by result before
    # asserting that no label is reused for two different tool results.
    unique_blocks = {tuple(b) for b in seen}
    assert len(unique_blocks) >= 2, "the task produced fewer than two evidence blocks"
    flat = [lbl for block in unique_blocks for lbl in block]
    assert len(flat) == len(set(flat)), (
        f"an evidence label names more than one value: {sorted(flat)}")
