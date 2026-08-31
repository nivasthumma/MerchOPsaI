"""Bounded agent runtime — CONTRACT §9, §10, §11.

This is the loop the model runs inside. Everything that matters sits BETWEEN
the model's tool request and the tool's execution:

    model requests tool
        -> argument validation
        -> policy evaluation (authorization, isolation, risk, limits)
        -> approval gate for HIGH risk (execution stops here)
        -> execution
        -> trace persistence

The model never touches the database, the provider, or the policy outcome.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.agent.output import check_grounding, parse as parse_output, to_findings
from app.agent.prompts.investigator_v1 import PROMPT_VERSION, SYSTEM_PROMPT
from app.audit.trace import record, redact, set_correlation_id
from app.config import get_settings
from app.failures import describe
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import FaultInjector
from app.llm import LLMProvider, get_provider
from app.models import (
    AgentMessage, AgentTask, Approval, TaskStatus, ToolCall, VerificationState,
)
from app.policy.engine import POLICY_VERSION, Decision, PolicyContext, evaluate
from app.tools.registry import (
    REGISTRY, execute_read_tool, registry_version, validate_arguments,
)


@dataclass
class Principal:
    """The authenticated session. This — never model output — is the source of
    authorization truth (CONTRACT §11, MerchantOps §54).

    `tenant_id` is first and has no default on purpose. A default would let a
    Principal be constructed without one, which is precisely the silent
    single-tenant assumption this field exists to remove: every call site has to
    say which tenant it is acting in.
    """
    tenant_id: str
    user_id: str
    merchant_id: str
    role: str
    permissions: list[str]


@dataclass
class RunOutcome:
    task: AgentTask
    status: TaskStatus
    answer: str
    approval: Approval | None = None
    findings: list = field(default_factory=list)


def evidence_index(session, task_id: str) -> dict[str, str]:
    """`E1` -> the tool_call that produced it, for one task.

    Derived rather than stored, in exactly the order the runtime rendered it:
    successful tool calls by sequence, evidence within each in order. MerchantOps
    §36 wants findings to reference specific evidence, and a citation scheme the
    model can use has to be short and stable — `E3` is citable in a sentence,
    `TC_9F2A...` is not.
    """
    rows = session.execute(text("""
        SELECT id, output FROM tool_calls
        WHERE task_id = :t AND success = true ORDER BY seq
    """), {"t": task_id}).mappings().all()
    index: dict[str, str] = {}
    n = 0
    for r in rows:
        for _ in (r["output"] or {}).get("evidence", []):
            n += 1
            index[f"E{n}"] = r["id"]
    return index


def _render_tool_result(result_dict: dict, evidence: list[dict],
                        start_id: int = 0) -> tuple[str, int]:
    """CONTRACT §36 — untrusted free text is wrapped, never interpolated bare.

    Each piece of evidence is labelled `E<n>` so the model can cite it in its
    §37 output. The numbering is per task and continues across tool calls.
    """
    safe = {k: v for k, v in result_dict.items() if k != "evidence"}
    body = json.dumps(safe, default=str, indent=2)
    blocks = [body]

    n = start_id
    labelled = []
    for ev in evidence:
        n += 1
        labelled.append(f'  {f"E{n}"}: {ev["key"]} = {ev["value"]}'
                        if not ev.get("untrusted") else
                        f'  {f"E{n}"}: {ev["key"]} = <see untrusted block below>')
    if labelled:
        blocks.append("EVIDENCE (cite these ids in your findings):\n" + "\n".join(labelled))

    for ev in evidence:
        if ev.get("untrusted"):
            blocks.append(
                f'<untrusted_merchant_data field="{ev["key"]}" source="{ev["source"]}">\n'
                f'{ev["value"]}\n'
                f'</untrusted_merchant_data>'
            )
    return "\n".join(blocks), n


class AgentRuntime:
    def __init__(self, session, principal: Principal,
                 provider: LLMProvider | None = None,
                 injector: FaultInjector | None = None,
                 frozen_tools: dict | None = None):
        self.session = session
        self.principal = principal
        self.provider = provider or get_provider()
        self.injector = injector or FaultInjector.disabled()
        self.settings = get_settings()
        # CONTRACT §28 RE-REASON: tool results are served from the recorded
        # trace instead of being re-executed.
        self.frozen_tools = frozen_tools
        # Evidence labels run across the whole task, not per tool call.
        self._evidence_seq = 0
        self._message_seq = 0

    # ------------------------------------------------------------------
    def run(self, request: str, *, scenario_id: str | None = None,
            is_replay: bool = False, replayed_from: str | None = None,
            incident_id: str | None = None,
            correlation_id: str | None = None) -> RunOutcome:
        s = self.settings
        started = time.monotonic()

        task = AgentTask(
            id=f"TASK_{uuid.uuid4().hex[:10].upper()}",
            merchant_id=self.principal.merchant_id, user_id=self.principal.user_id,
            request=request, status=TaskStatus.RUNNING,
            agent_version=s.agent_version, model_version=self.provider.model,
            model_provider=self.provider.name,
            prompt_version=PROMPT_VERSION,
            # §41. Derived, so it cannot drift from what actually ran.
            tool_registry_version=registry_version(),
            policy_version=POLICY_VERSION,
            workflow_version=s.workflow_version,
            scenario_id=scenario_id,
            is_replay=is_replay, replayed_from=replayed_from,
            # Set at creation, not afterwards. `app.audit.trace.record` reads
            # incident_id off the task as each event is written, and audit rows
            # are immutable by database trigger -- a task bound to its incident
            # after the run leaves every event of that run off the incident's
            # trace, permanently (MerchantOps §58).
            incident_id=incident_id,
        )
        self.session.add(task)
        self.session.flush()
        # §47/§58. One id ties every event of this run — and of the incident
        # that dispatched it — into a single trace.
        self._correlation_id = correlation_id or f"COR_{uuid.uuid4().hex[:12].upper()}"
        set_correlation_id(self._correlation_id)
        record(self.session, task, "task_created",
               {"request": request, "provider": self.provider.name,
                "model": self.provider.model, "replay": is_replay})

        tools = [spec.to_anthropic_tool() for spec in REGISTRY.values()]
        messages: list[dict] = []
        self._say(task, messages, 0, "user", request)
        seq = 0
        answer = ""
        approval: Approval | None = None

        for turn_no in range(s.max_llm_turns_per_task):
            # ---- budget (CONTRACT §10) -------------------------------
            if time.monotonic() - started > s.max_wall_clock_seconds:
                return self._abort(task, "wall clock", started)
            if seq >= s.max_tool_calls_per_task:
                return self._abort(task, "tool call limit", started)

            task.llm_turn_count = turn_no + 1
            turn = self.provider.turn(system=SYSTEM_PROMPT, messages=messages, tools=tools)
            record(self.session, task, "llm_turn",
                   {"turn": turn_no + 1, "stop_reason": turn.stop_reason,
                    "requested_tools": [t.name for t in turn.tool_requests],
                    "usage": turn.usage})

            if not turn.wants_tools:
                answer = turn.text
                # Recorded even though the loop ends here. It was never appended
                # to `messages` because nothing reads it afterwards — which is
                # exactly why the one message a person most wants to see was
                # the one the transcript would have been missing.
                self._say(task, messages, turn_no + 1, "assistant", turn.text)
                break

            assistant_blocks = [{"type": "tool_use", "id": t.id, "name": t.name,
                                 "input": t.arguments} for t in turn.tool_requests]
            if turn.text:
                assistant_blocks.insert(0, {"type": "text", "text": turn.text})
            self._say(task, messages, turn_no + 1, "assistant", assistant_blocks)

            result_blocks = []
            for req in turn.tool_requests:
                seq += 1
                tc, payload, halted, approval_obj = self._handle_tool(task, req, seq)
                if approval_obj is not None:
                    approval = approval_obj
                block = {"type": "tool_result", "tool_use_id": req.id,
                         "content": payload["rendered"], "_structured": payload["structured"]}
                if not payload["structured"].get("success", False):
                    block["is_error"] = True
                result_blocks.append(block)
                if halted:
                    # HIGH-risk action requires approval: stop the loop here.
                    self._say(task, messages, turn_no + 1, "user", result_blocks)
                    task.status = TaskStatus.AWAITING_APPROVAL
                    task.tool_call_count = seq
                    task.duration_ms = int((time.monotonic() - started) * 1000)
                    answer = payload["structured"].get("data", {}).get("message", "")
                    task.final_answer = answer
                    self.session.flush()
                    record(self.session, task, "awaiting_approval",
                           {"approval_id": approval.id if approval else None})
                    return RunOutcome(task, task.status, answer, approval)

            self._say(task, messages, turn_no + 1, "user", result_blocks)

        task.tool_call_count = seq
        task.duration_ms = int((time.monotonic() - started) * 1000)

        # ---- §37 structured output ---------------------------------------
        prose, output, problem = self._structured_output(task, answer)
        answer = prose
        task.final_answer = prose
        task.findings = self._derive_findings(task, prose) + (
            to_findings(output, evidence_index(self.session, task.id)) if output else [])
        if task.status is TaskStatus.RUNNING:
            task.status = TaskStatus.COMPLETED
        if problem is not None:
            # A malformed or ungrounded answer is a failed task, not a task with
            # a caveat. Reporting it as completed would put an unvalidated claim
            # in front of a merchant with a green tick beside it.
            task.status = TaskStatus.FAILED
            task.failure_code = problem.code
        self.session.flush()
        record(self.session, task, "task_completed",
               {"status": task.status.value, "tool_calls": seq,
                "duration_ms": task.duration_ms,
                "failure_code": task.failure_code,
                "failure": describe(task.failure_code,
                                    correlation_id=self._correlation_id)})
        set_correlation_id(None)
        return RunOutcome(task, task.status, answer, approval, task.findings)

    # ------------------------------------------------------------------
    def _say(self, task: AgentTask, messages: list[dict], turn_no: int,
             role: str, content) -> None:
        """Append to the conversation and record it in one step.

        One call site for both, deliberately. Appending in one place and
        persisting in another is how a transcript comes to be missing the
        message that mattered — and the whole value of this table is that it is
        what the model actually saw, not an approximation assembled later.
        """
        messages.append({"role": role, "content": content})

        # `_structured` is our own parsed copy of a tool result, attached for
        # the planner's benefit and never sent to a model. It is already stored
        # on tool_calls.output; keeping a second copy here would double the
        # transcript's size to record nothing the model saw.
        if isinstance(content, list):
            stored = [{k: v for k, v in b.items() if k != "_structured"}
                      if isinstance(b, dict) else b for b in content]
        else:
            stored = [{"type": "text", "text": str(content)}]

        blob = json.dumps(stored, default=str)
        self._message_seq += 1
        self.session.add(AgentMessage(
            id=f"MSG_{uuid.uuid4().hex[:10].upper()}", task_id=task.id,
            seq=self._message_seq, turn=turn_no, role=role,
            content=redact(stored),
            contains_untrusted="<untrusted_merchant_data" in blob,
            char_count=len(blob),
        ))
        self.session.flush()

    # ------------------------------------------------------------------
    def _structured_output(self, task: AgentTask, answer: str):
        """Extract, validate and record §37's object. Returns (prose, output, problem)."""
        prose, output, problem = parse_output(answer)

        if output is not None and problem is None:
            problem = check_grounding(output, set(evidence_index(self.session, task.id)))

        if problem is not None:
            record(self.session, task, "agent_output_rejected",
                   {"code": problem.code, "detail": problem.detail,
                    "offending": problem.offending})
            return prose, None, problem

        if output is None:
            # No block at all. Recorded, not fatal: a task that answered a
            # question without proposing anything is a legitimate outcome, and
            # the deterministic planner is not the only provider this runs on.
            record(self.session, task, "agent_output_absent", {})
            return prose, None, None

        task.intent = output.intent[:64]
        task.recommendation = {
            "type": output.recommendation.type,
            "detail": output.recommendation.detail,
        } if output.recommendation else None
        # §38: agent state, stored beside the financial record and never mixed
        # into it. `confidence` gates nothing and `requires_human` may only
        # raise the bar -- see app/agent/output.py.
        task.agent_confidence = output.confidence
        task.model_requires_human = output.requires_human
        record(self.session, task, "agent_output",
               {"intent": output.intent, "confidence": output.confidence,
                "requires_human": output.requires_human,
                "findings": len(output.findings),
                "recommendation": task.recommendation})
        return prose, output, None

    # ------------------------------------------------------------------
    def _abort(self, task: AgentTask, why: str, started: float) -> RunOutcome:
        task.status = TaskStatus.ABORTED_BUDGET
        task.failure_code = "BUDGET_EXCEEDED"
        task.final_answer = f"Task aborted: {why} budget exceeded. Partial trace preserved."
        task.duration_ms = int((time.monotonic() - started) * 1000)
        self.session.flush()
        record(self.session, task, "budget_exceeded", {"limit": why})
        return RunOutcome(task, task.status, task.final_answer)

    # ------------------------------------------------------------------
    def _handle_tool(self, task: AgentTask, req, seq: int):
        """Returns (tool_call, payload, halted, approval)."""
        t0 = time.monotonic()
        spec = REGISTRY.get(req.name)

        tc = ToolCall(id=f"TC_{uuid.uuid4().hex[:10].upper()}", task_id=task.id, seq=seq,
                      tool_name=req.name, input=req.arguments)

        # ---- unregistered tool (CONTRACT §10) --------------------------
        if spec is None:
            tc.success = False
            tc.error_code = "TOOL_UNAVAILABLE"
            tc.output = {"error": f"Tool '{req.name}' is not registered."}
            tc.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.add(tc)
            self.session.flush()
            record(self.session, task, "tool_rejected", {"tool": req.name, "reason": "unregistered"})
            structured = {"success": False, "error_code": "TOOL_UNAVAILABLE",
                          "data": {"error": f"Tool '{req.name}' is not registered."}}
            return tc, {"rendered": json.dumps(structured), "structured": structured}, False, None

        # ---- argument validation (CONTRACT §13) ------------------------
        # MUST precede policy evaluation. The policy engine queries the
        # database using these values; passing an unvalidated model-supplied
        # value into that query is both a crash and an injection surface.
        # CONTRACT §33 requires malformed input be rejected before any
        # external call -- this is the gate that does it.
        ok, arg_err = validate_arguments(spec, req.arguments)
        if not ok:
            tc.success = False
            tc.error_code = "TOOL_INVALID_ARGUMENT"
            tc.risk_level = spec.risk_class.value
            tc.output = {"error": arg_err}
            tc.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.add(tc)
            self.session.flush()
            record(self.session, task, "tool_rejected",
                   {"tool": req.name, "reason": "invalid_arguments", "detail": arg_err})
            structured = {"success": False, "error_code": "TOOL_INVALID_ARGUMENT",
                          "data": {"error": arg_err}}
            return tc, {"rendered": json.dumps(structured), "structured": structured}, False, None

        # ---- policy (CONTRACT §20) -------------------------------------
        ctx = PolicyContext(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id, merchant_id=self.principal.merchant_id,
            role=self.principal.role, permissions=self.principal.permissions,
            tool_name=req.name, risk_level=spec.risk_class.value, arguments=req.arguments,
        )
        pol = evaluate(self.session, ctx)
        tc.risk_level = pol.risk_level
        tc.policy_decision = pol.decision.value
        record(self.session, task, "policy_decision",
               {"tool": req.name, "decision": pol.decision.value, "rule": pol.rule,
                "reason": pol.reason, "arguments": req.arguments})

        if pol.decision is Decision.DENY:
            tc.success = False
            tc.error_code = "POLICY_DENIED"
            tc.output = {"policy": pol.as_dict()}
            tc.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.add(tc)
            self.session.flush()
            structured = {"success": False, "error_code": "POLICY_DENIED",
                          "policy_decision": "DENY",
                          "data": {"reason": pol.reason, "rule": pol.rule}}
            return tc, {"rendered": json.dumps(structured), "structured": structured}, False, None

        if pol.decision in (Decision.REQUIRE_APPROVAL, Decision.REQUIRE_DUAL_APPROVAL):
            approval = self._create_approval(task, req, pol)
            tc.success = False
            tc.error_code = None
            tc.output = {"policy": pol.as_dict(), "approval_id": approval.id}
            tc.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.add(tc)
            self.session.flush()
            n = approval.required_signatures
            who = "human approval" if n == 1 else f"{n} separate human approvers"
            msg = (f"This action requires {who}. Approval request {approval.id} "
                   f"has been created and execution is paused. No external call was made.")
            structured = {"success": False, "error_code": None,
                          "policy_decision": pol.decision.value, "approval_required": True,
                          "required_signatures": n,
                          "approval_id": approval.id, "data": {"message": msg}}
            return tc, {"rendered": json.dumps(structured), "structured": structured}, True, approval

        # ---- execute (LOW risk / ALLOW) --------------------------------
        result = execute_read_tool(
            self.session, req.name, self.principal.merchant_id, req.arguments,
            frozen=self._frozen_for(seq, req.name),
            # Some §18 verification tools answer questions about PROVIDER state,
            # so they need the adapter. It is passed here rather than built
            # inside the tool, so the fault injector reaches them too.
            adapter=get_adapter(self.session, self.injector),
        )
        tc.success = result.success
        tc.error_code = result.error_code
        tc.output = result.model_dump()
        tc.duration_ms = int((time.monotonic() - t0) * 1000)
        self.session.add(tc)
        self.session.flush()
        record(self.session, task, "tool_call",
               {"seq": seq, "tool": req.name, "success": result.success,
                "tool_call_id": tc.id, "duration_ms": tc.duration_ms})

        structured = result.model_dump()
        structured["tool_call_id"] = tc.id
        rendered, self._evidence_seq = _render_tool_result(
            structured, list(structured.get("evidence", [])), self._evidence_seq)
        return tc, {"rendered": rendered, "structured": structured}, False, None

    # ------------------------------------------------------------------
    def _frozen_for(self, seq: int, name: str):
        if not self.frozen_tools:
            return None
        return self.frozen_tools.get(f"{seq}:{name}") or self.frozen_tools.get(name)

    def _create_approval(self, task: AgentTask, req, pol) -> Approval:
        s = self.settings
        ap = Approval(
            id=f"APR_{uuid.uuid4().hex[:10].upper()}", task_id=task.id,
            merchant_id=self.principal.merchant_id, action_type=req.name,
            action_payload=req.arguments,
            evidence=self._collect_evidence(task), risk_level=pol.risk_level,
            decision="PENDING",
            # Fixed at proposal time. A later policy change must not quietly
            # reduce what an in-flight action needs.
            required_signatures=pol.required_signatures,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=s.approval_ttl_seconds),
        )
        self.session.add(ap)
        self.session.flush()
        record(self.session, task, "approval_requested",
               {"approval_id": ap.id, "action": req.name, "payload": req.arguments,
                "risk_level": pol.risk_level,
                "required_signatures": ap.required_signatures,
                "risk": pol.risk.as_dict() if pol.risk else None,
                "expires_at": ap.expires_at.isoformat()})
        return ap

    def _collect_evidence(self, task: AgentTask) -> list:
        rows = self.session.execute(text("""
            SELECT id, tool_name, output FROM tool_calls
            WHERE task_id = :t AND success = true ORDER BY seq
        """), {"t": task.id}).mappings().all()
        out = []
        for r in rows:
            for ev in (r["output"] or {}).get("evidence", []):
                out.append({"tool_call_id": r["id"], "tool": r["tool_name"], **ev})
        return out

    def _derive_findings(self, task: AgentTask, answer: str) -> list:
        """Build the typed Finding list (CONTRACT §14 amended) from what the
        tools actually returned. Each OBSERVED finding cites the tool_call that
        produced it, which is what makes grounding computable."""
        rows = self.session.execute(text("""
            SELECT id, tool_name, output FROM tool_calls
            WHERE task_id = :t AND success = true ORDER BY seq
        """), {"t": task.id}).mappings().all()
        findings = []
        for r in rows:
            for ev in (r["output"] or {}).get("evidence", []):
                if ev.get("untrusted"):
                    continue
                findings.append({
                    "claim": f"{ev['key']} = {ev['value']}",
                    "kind": "OBSERVED", "evidence_refs": [r["id"]],
                    "metric": ev["key"], "value": ev["value"],
                })
        if answer:
            findings.append({
                "claim": answer, "kind": "INFERRED",
                "evidence_refs": [r["id"] for r in rows], "metric": None, "value": None,
            })
        return findings
