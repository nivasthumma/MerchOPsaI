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

from app.agent.prompts.investigator_v1 import PROMPT_VERSION, SYSTEM_PROMPT
from app.audit.trace import record
from app.config import get_settings
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import FaultInjector
from app.llm import LLMProvider, get_provider
from app.models import (
    AgentTask, Approval, TaskStatus, ToolCall, VerificationState,
)
from app.policy.engine import Decision, PolicyContext, evaluate
from app.tools.registry import REGISTRY, execute_read_tool, validate_arguments


@dataclass
class Principal:
    """The authenticated session. This — never model output — is the source of
    authorization truth (CONTRACT §11)."""
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


def _render_tool_result(result_dict: dict, evidence: list[dict]) -> str:
    """CONTRACT §36 — untrusted free text is wrapped, never interpolated bare."""
    safe = {k: v for k, v in result_dict.items() if k != "evidence"}
    body = json.dumps(safe, default=str, indent=2)
    blocks = [body]
    for ev in evidence:
        if ev.get("untrusted"):
            blocks.append(
                f'<untrusted_merchant_data field="{ev["key"]}" source="{ev["source"]}">\n'
                f'{ev["value"]}\n'
                f'</untrusted_merchant_data>'
            )
    return "\n".join(blocks)


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

    # ------------------------------------------------------------------
    def run(self, request: str, *, scenario_id: str | None = None,
            is_replay: bool = False, replayed_from: str | None = None) -> RunOutcome:
        s = self.settings
        started = time.monotonic()

        task = AgentTask(
            id=f"TASK_{uuid.uuid4().hex[:10].upper()}",
            merchant_id=self.principal.merchant_id, user_id=self.principal.user_id,
            request=request, status=TaskStatus.RUNNING,
            agent_version=s.agent_version, model_version=self.provider.model,
            prompt_version=PROMPT_VERSION, scenario_id=scenario_id,
            is_replay=is_replay, replayed_from=replayed_from,
        )
        self.session.add(task)
        self.session.flush()
        record(self.session, task, "task_created",
               {"request": request, "provider": self.provider.name,
                "model": self.provider.model, "replay": is_replay})

        tools = [spec.to_anthropic_tool() for spec in REGISTRY.values()]
        messages: list[dict] = [{"role": "user", "content": request}]
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
                break

            assistant_blocks = [{"type": "tool_use", "id": t.id, "name": t.name,
                                 "input": t.arguments} for t in turn.tool_requests]
            if turn.text:
                assistant_blocks.insert(0, {"type": "text", "text": turn.text})
            messages.append({"role": "assistant", "content": assistant_blocks})

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
                    messages.append({"role": "user", "content": result_blocks})
                    task.status = TaskStatus.AWAITING_APPROVAL
                    task.tool_call_count = seq
                    task.duration_ms = int((time.monotonic() - started) * 1000)
                    answer = payload["structured"].get("data", {}).get("message", "")
                    task.final_answer = answer
                    self.session.flush()
                    record(self.session, task, "awaiting_approval",
                           {"approval_id": approval.id if approval else None})
                    return RunOutcome(task, task.status, answer, approval)

            messages.append({"role": "user", "content": result_blocks})

        task.tool_call_count = seq
        task.duration_ms = int((time.monotonic() - started) * 1000)
        task.final_answer = answer
        task.findings = self._derive_findings(task, answer)
        if task.status is TaskStatus.RUNNING:
            task.status = TaskStatus.COMPLETED
        self.session.flush()
        record(self.session, task, "task_completed",
               {"status": task.status.value, "tool_calls": seq,
                "duration_ms": task.duration_ms})
        return RunOutcome(task, task.status, answer, approval, task.findings)

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

        if pol.decision is Decision.REQUIRE_APPROVAL:
            approval = self._create_approval(task, req, pol)
            tc.success = False
            tc.error_code = None
            tc.output = {"policy": pol.as_dict(), "approval_id": approval.id}
            tc.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.add(tc)
            self.session.flush()
            msg = (f"This action requires human approval. Approval request {approval.id} "
                   f"has been created and execution is paused. No external call was made.")
            structured = {"success": False, "error_code": None,
                          "policy_decision": "REQUIRE_APPROVAL", "approval_required": True,
                          "approval_id": approval.id, "data": {"message": msg}}
            return tc, {"rendered": json.dumps(structured), "structured": structured}, True, approval

        # ---- execute (LOW risk / ALLOW) --------------------------------
        result = execute_read_tool(
            self.session, req.name, self.principal.merchant_id, req.arguments,
            frozen=self._frozen_for(seq, req.name),
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
        rendered = _render_tool_result(structured, [e for e in structured.get("evidence", [])])
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
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=s.approval_ttl_seconds),
        )
        self.session.add(ap)
        self.session.flush()
        record(self.session, task, "approval_requested",
               {"approval_id": ap.id, "action": req.name, "payload": req.arguments,
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
