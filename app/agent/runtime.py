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
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app import context as exec_context
from app.agent.output import check_grounding, to_findings
from app.agent.output import parse as parse_output
from app.agent.prompts.investigator_v1 import PROMPT_VERSION, SYSTEM_PROMPT
from app.agent.provenance import (
    AIMode,
    configuration_version,
    initial_mode,
    policy_input_hash,
)
from app.audit.trace import (
    correlation_scope,
    current_correlation_id,
    record,
    redact,
)
from app.config import get_settings
from app.db import checkpoint
from app.failures import describe
from app.integrations.razorpay.adapter import get_adapter
from app.integrations.razorpay.faults import FaultInjector
from app.llm import LLMProvider, get_provider
from app.llm.base import ModelUnavailable
from app.llm.deterministic import DeterministicProvider
from app.models import (
    AgentMessage,
    AgentTask,
    Approval,
    TaskStatus,
    ToolCall,
)
from app.observability.logs import get_logger
from app.policy.engine import POLICY_VERSION, Decision, PolicyContext, evaluate
from app.tools.registry import _NEEDS_ADAPTER as NEEDS_ADAPTER
from app.tools.registry import (
    REGISTRY,
    execute_read_tool,
    registry_version,
    validate_arguments,
)

log = get_logger("merchantops.agent")


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


class AgentRuntimeError(Exception):
    """An unhandled error ended a run, and the trace was preserved anyway.

    The runtime classifies every failure it anticipates — a denied policy, an
    expired approval, an exhausted budget — and each of those is an ordinary
    return with a failure code. This is the other kind: a provider that raised,
    a connection that dropped, a bug. Before it reaches the caller the task is
    marked FAILED with INTERNAL_ERROR and the partial trace is committed, so an
    operator can still open the run and see how far it got.

    `task_id` is what makes that trace reachable, which is the only reason this
    exists rather than letting the original error propagate. `persisted` is
    False when even the crash record could not be written — the session was
    already unusable — and saying so is better than implying a trace exists.
    """

    def __init__(self, task_id: str | None, detail: str, *, persisted: bool = True):
        super().__init__(detail)
        self.task_id = task_id
        self.detail = detail
        self.persisted = persisted


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
                 frozen_tools: dict | None = None,
                 on_phase=None):
        self.session = session
        self.principal = principal
        # MerchantOps v2 §20. Called with a phase name when the run reaches one,
        # so an incident can move through EVIDENCE_COLLECTING and DIAGNOSING at
        # the moment the work happens rather than being back-dated afterwards.
        #
        # A callback rather than the runtime moving the incident itself: this
        # module runs for incident-dispatched tasks and for merchant questions
        # that have no incident, and it has no business knowing which. The
        # caller that owns an incident is the caller that may move one.
        self.on_phase = on_phase
        # A model that cannot even be constructed is unavailable, not a crash:
        # the run proceeds on the planner and the task says so.
        self._unavailable_reason: str | None = None
        if provider is None:
            try:
                provider = get_provider()
            except Exception as exc:
                if not get_settings().llm_fallback_enabled:
                    raise
                self._unavailable_reason = f"{type(exc).__name__}: {exc}"
                provider = DeterministicProvider()
        self.provider = provider
        self.ai_mode = (AIMode.AI_UNAVAILABLE_FALLBACK if self._unavailable_reason
                        else initial_mode(provider.name))
        # Turns the configured model actually produced. Decides which fallback
        # a failure is: before any, the model was never available to this run.
        self._model_turns = 0
        self.injector = injector or FaultInjector.disabled()
        self.settings = get_settings()
        # CONTRACT §28 RE-REASON: tool results are served from the recorded
        # trace instead of being re-executed.
        self.frozen_tools = frozen_tools
        # Evidence labels run across the whole task, not per tool call.
        self._evidence_seq = 0
        self._message_seq = 0
        # Set by `_run`; declared here so the failure boundary can read them
        # even when the run died before either was assigned.
        self._task: AgentTask | None = None
        self._correlation_id: str | None = None

    def _phase(self, name: str) -> None:
        """Tell the caller the run reached a phase — v2 §20.

        Never fatal. A caller that cannot move its incident (an illegal
        transition, an incident somebody closed underneath us) must not take
        the agent run down with it: the investigation is the work, and the
        status is a description of it.
        """
        if self.on_phase is None:
            return
        try:
            self.on_phase(name)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "phase hook failed for %s", name, exc_info=True)

    # ------------------------------------------------------------------
    def run(self, request: str, **kwargs) -> RunOutcome:
        """Run the loop, and never lose the trace if it breaks.

        Everything below this method writes to a session the caller opened and
        commits at the end of the request. That is right while the run is going
        well and wrong the moment it is not: an exception here would roll the
        whole request back, taking the task row, every tool call and every audit
        event with it — a run that happened, cost money and left no evidence.

        So an unhandled error is caught, recorded, committed, and re-raised as
        an AgentRuntimeError carrying the task id. The failure still fails; it
        just stops being invisible.
        """
        self._task = None
        # Inherited before it is minted. The request middleware has already put
        # an id on this context, so a run dispatched by an HTTP call joins that
        # request's trace instead of starting a second one beside it -- and the
        # log line for the response then carries the same id as the audit rows
        # the run wrote. An explicit argument still wins: an incident-dispatched
        # task belongs to the incident's trace.
        correlation = (kwargs.get("correlation_id")
                       or current_correlation_id()
                       or f"COR_{uuid.uuid4().hex[:12].upper()}")
        kwargs["correlation_id"] = correlation

        # A scope, so whatever the caller had is put back. Clearing to None
        # would leave the rest of the request logged as belonging to no trace.
        with correlation_scope(correlation):
            log.info("task_started", extra={"merchant_id": self.principal.merchant_id,
                                            "user_id": self.principal.user_id,
                                            "provider": self.provider.name,
                                            "model": self.provider.model})
            try:
                # Every audit row the run writes says AGENT (app/context.py),
                # inside whoever started it -- same scope, same trace.
                with exec_context.as_agent(None):
                    return self._run(request, **kwargs)
            except Exception as exc:
                raise self._crash(exc) from exc

    # ------------------------------------------------------------------
    def _crash(self, exc: Exception) -> AgentRuntimeError:
        """Preserve what the run produced, then hand back a raisable error."""
        detail = f"{type(exc).__name__}: {exc}"
        task = self._task
        if task is None:
            # Nothing had been written yet, so there is no trace to save.
            self.session.rollback()
            log.error("task_crashed_before_persist", exc_info=exc)
            return AgentRuntimeError(None, detail, persisted=False)

        try:
            task.status = TaskStatus.FAILED
            task.failure_code = "INTERNAL_ERROR"
            task.final_answer = ("This run stopped on an unhandled error. The partial "
                                 "trace up to that point is preserved.")
            self.session.flush()
            # `redact` because the message is whatever the raising library chose
            # to put in it, and provider errors quote the request they failed on.
            record(self.session, task, "task_crashed",
                   {"error": redact(detail),
                    "failure": describe("INTERNAL_ERROR",
                                        correlation_id=self._correlation_id)})
            self.session.commit()
            # Also to stdout. The audit row is per-tenant and behind
            # authentication; the operator who has to fix this reads logs, and
            # until now an unhandled error reached them as nothing at all.
            log.error("task_crashed", extra={"task_id": task.id}, exc_info=exc)
            return AgentRuntimeError(task.id, detail)
        except Exception:
            # The session itself is unusable — a database error, most likely.
            # Give up on the trace rather than mask the original failure, and
            # tell the caller that is what happened. The log is now the only
            # record this run existed, which is the reason it is written here.
            self.session.rollback()
            log.error("task_crashed_unrecorded",
                      extra={"task_id": task.id, "detail": redact(detail)})
            return AgentRuntimeError(task.id, detail, persisted=False)

    # ------------------------------------------------------------------
    def _run(self, request: str, *, scenario_id: str | None = None,
             is_replay: bool = False, replayed_from: str | None = None,
             incident_id: str | None = None,
             correlation_id: str | None = None,
             existing_task: AgentTask | None = None) -> RunOutcome:
        s = self.settings
        started = time.monotonic()

        # `existing_task` is a row a worker has already claimed (ADR-0045). The
        # §41 provenance is written HERE either way, not at enqueue: it records
        # what actually ran, and the provider can change between a task being
        # accepted and it starting -- `POST /config/llm-provider` exists to do
        # exactly that. A model version written at enqueue would be a prediction
        # stored where a measurement belongs.
        provenance = dict(
            status=TaskStatus.RUNNING,
            agent_version=s.agent_version,
            model_version=self.provider.model,
            model_provider=self.provider.name,
            prompt_version=PROMPT_VERSION,
            # §41. Derived, so it cannot drift from what actually ran.
            tool_registry_version=registry_version(),
            policy_version=POLICY_VERSION,
            workflow_version=s.workflow_version,
            configuration_version=configuration_version(s),
            ai_mode=self.ai_mode.value,
        )

        if existing_task is not None:
            task = existing_task
            for field, value in provenance.items():
                setattr(task, field, value)
            # Not re-derived from the argument: the row is the authority on what
            # was asked, and a worker passing a different string would silently
            # run something other than what was accepted.
            request = task.request
        else:
            task = AgentTask(
                id=f"TASK_{uuid.uuid4().hex[:10].upper()}",
                merchant_id=self.principal.merchant_id,
                user_id=self.principal.user_id,
                request=request,
                scenario_id=scenario_id,
                is_replay=is_replay, replayed_from=replayed_from,
                # Set at creation, not afterwards. `app.audit.trace.record` reads
                # incident_id off the task as each event is written, and audit rows
                # are immutable by database trigger -- a task bound to its incident
                # after the run leaves every event of that run off the incident's
                # trace, permanently (MerchantOps §58).
                incident_id=incident_id,
                **provenance,
            )
            self.session.add(task)
        self.session.flush()
        # Visible to `run`'s except clause from here on. Before this line there
        # is no task to attach a failure to; after it, every failure has one.
        self._task = task
        exec_context.update(actor=f"agent:{task.id}", task_id=task.id,
                            incident_id=task.incident_id)
        # §47/§58. One id ties every event of this run — and of the incident
        # that dispatched it — into a single trace.
        # Always supplied by `run`, which resolves inheritance and scoping.
        self._correlation_id = correlation_id or current_correlation_id() or ""
        record(self.session, task, "task_created",
               {"request": request, "provider": self.provider.name,
                "model": self.provider.model, "replay": is_replay,
                "ai_mode": self.ai_mode.value})
        if self._unavailable_reason is not None:
            record(self.session, task, "llm_fallback",
                   {"mode": self.ai_mode.value, "turn": 0,
                    "reason": redact(self._unavailable_reason)})

        tools = [spec.to_anthropic_tool() for spec in REGISTRY.values()]
        messages: list[dict] = []
        self._say(task, messages, 0, "user", request)
        seq = 0
        answer = ""
        approval: Approval | None = None

        # Not `max_wall_clock_seconds`: the host may allow less than we asked
        # for, and a deadline it will not honour is not one. See
        # Settings.effective_wall_clock_seconds.
        deadline = s.effective_wall_clock_seconds

        for turn_no in range(s.max_llm_turns_per_task):
            # ---- budget (CONTRACT §10) -------------------------------
            elapsed = time.monotonic() - started
            if elapsed > deadline:
                return self._abort(task, "wall clock", started)
            if seq >= s.max_tool_calls_per_task:
                return self._abort(task, "tool call limit", started)

            task.llm_turn_count = turn_no + 1
            # No model call with a write transaction open (app/boundaries.py).
            # Everything written so far -- the task, its messages, its tool
            # calls, the audit rows -- is history that already happened, so it
            # is committed rather than held, with its locks, for as long as the
            # model takes to answer. It also means a run killed mid-turn leaves
            # its trace behind instead of rolling it back.
            checkpoint(self.session)
            # The turn gets what is left, so the budget bounds the run rather
            # than only the gaps between calls.
            try:
                turn = self.provider.turn(system=SYSTEM_PROMPT, messages=messages,
                                          tools=tools, timeout=deadline - elapsed)
                self._model_turns += 1
            except ModelUnavailable as exc:
                if not s.llm_fallback_enabled or self.provider.name == "deterministic":
                    raise
                turn = self._fall_back(task, exc, turn_no + 1, messages, tools,
                                       deadline - elapsed)
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

            # Replay what the model actually produced, when the provider can
            # say. Rebuilding the turn from `text` + `tool_requests` drops
            # `thinking` blocks, and a model running adaptive thinking requires
            # them back unchanged on the turns that carry tool calls -- so the
            # reconstruction is only correct for a provider that never thinks.
            assistant_blocks = list(turn.echo_blocks)
            if not assistant_blocks:
                assistant_blocks = [{"type": "tool_use", "id": t.id, "name": t.name,
                                     "input": t.arguments} for t in turn.tool_requests]
                if turn.text:
                    assistant_blocks.insert(0, {"type": "text", "text": turn.text})
            self._say(task, messages, turn_no + 1, "assistant", assistant_blocks)

            result_blocks = []
            for req in turn.tool_requests:
                if seq == 0:
                    # v2 §20: the first tool call is the moment evidence starts
                    # arriving, and the difference between a run that was
                    # dispatched and one that is working.
                    self._phase("evidence_collecting")
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
        log.info("task_finished", extra={"task_id": task.id, "status": task.status.value,
                                         "failure_code": task.failure_code,
                                         "tool_calls": seq, "llm_turns": task.llm_turn_count,
                                         "duration_ms": task.duration_ms})
        return RunOutcome(task, task.status, answer, approval, task.findings)

    # ------------------------------------------------------------------
    def _fall_back(self, task: AgentTask, exc: Exception, turn_no: int,
                   messages: list[dict], tools: list[dict], timeout: float):
        """The model failed. Finish on the planner, and say so.

        Which fallback it is depends on whether the model produced anything: a
        failure on the first turn is a model this run never had, not one that
        failed partway. Recorded before the planner's turn, on the task and in
        the trail, so nothing downstream can present what follows as the
        model's work.
        """
        self.ai_mode = (AIMode.AI_FAILED_FALLBACK if self._model_turns
                        else AIMode.AI_UNAVAILABLE_FALLBACK)
        record(self.session, task, "llm_fallback",
               {"mode": self.ai_mode.value, "turn": turn_no,
                "from_provider": self.provider.name, "from_model": self.provider.model,
                "reason": redact(f"{type(exc).__name__}: {exc}")})
        log.warning("llm_fallback", extra={"task_id": task.id,
                                           "from_provider": self.provider.name})
        self.provider = DeterministicProvider()
        task.ai_mode = self.ai_mode.value
        self.session.flush()
        return self.provider.turn(system=SYSTEM_PROMPT, messages=messages, tools=tools,
                                  timeout=timeout)

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

        # v2 §20: a well-formed output block means the run is weighing what it
        # gathered. Raised here rather than on the first finding because a run
        # that reached a conclusion of "nothing" still diagnosed.
        self._phase("diagnosing")

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
                "reason": pol.reason, "arguments": req.arguments,
                "duration_ms": pol.duration_ms})

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
        frozen = self._frozen_for(seq, req.name)
        if req.name in NEEDS_ADAPTER and frozen is None:
            if self.frozen_tools is not None:
                # A replay reads only what was recorded. A provider read with no
                # recorded result would be a live external call from a replay,
                # which CONTRACT §28 forbids however harmless the read looks.
                tc.success = False
                tc.error_code = "TOOL_UNAVAILABLE"
                tc.output = {"error": "No recorded result for this provider read; "
                                      "a replay never calls the provider."}
                tc.duration_ms = int((time.monotonic() - t0) * 1000)
                self.session.add(tc)
                self.session.flush()
                record(self.session, task, "tool_rejected",
                       {"tool": req.name, "reason": "replay_provider_read"})
                structured = {"success": False, "error_code": "TOOL_UNAVAILABLE",
                              "data": tc.output}
                return tc, {"rendered": json.dumps(structured),
                            "structured": structured}, False, None
            # The provider is about to be asked. What the run has written so
            # far is committed first (app/boundaries.py).
            checkpoint(self.session)
        result = execute_read_tool(
            self.session, req.name, self.principal.merchant_id, req.arguments,
            frozen=frozen,
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
            expires_at=datetime.now(UTC) + timedelta(seconds=s.approval_ttl_seconds),
            # The policy snapshot the human is shown and approves against.
            # Execution re-evaluates current policy anyway; this records which
            # one was in force, and pins the exact request that was approved.
            policy_version=POLICY_VERSION,
            policy_input_hash=policy_input_hash(
                action_type=req.name, arguments=req.arguments,
                merchant_id=self.principal.merchant_id, risk_level=pol.risk_level),
            policy_decision=pol.decision.value,
            policy_rule=pol.rule,
        )
        self.session.add(ap)
        self.session.flush()
        record(self.session, task, "approval_requested",
               {"approval_id": ap.id, "action": req.name, "payload": req.arguments,
                "risk_level": pol.risk_level,
                "required_signatures": ap.required_signatures,
                "risk": pol.risk.as_dict() if pol.risk else None,
                "expires_at": ap.expires_at.isoformat(),
                "policy_version": ap.policy_version})
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
                    # The evidence says what it is (tools/contracts.py
                    # EvidenceKind): a computed figure is DERIVED, a provider
                    # reference EXECUTED, a settled read-back VERIFIED. Rows
                    # recorded before kinds existed read as OBSERVED.
                    "kind": ev.get("kind") or "OBSERVED", "evidence_refs": [r["id"]],
                    "metric": ev["key"], "value": ev["value"],
                })
        if answer:
            findings.append({
                "claim": answer, "kind": "INFERRED",
                "evidence_refs": [r["id"] for r in rows], "metric": None, "value": None,
            })
        return findings
