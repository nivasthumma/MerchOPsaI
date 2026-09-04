"""FastAPI surface — CONTRACT §41 (reverify added by ADR-0008 #3).

Every endpoint enforces authentication and merchant isolation server-side. The
frontend is never the authority (CONTRACT §20, §41).
"""
from __future__ import annotations

import hmac
import os
from datetime import timedelta
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select, text

from app import shared_state
from app.agent.approval import ApprovalError, approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime, AgentRuntimeError, Principal
from app.api import schemas
from app.api.security import (
    DEV_SECRET_IN_USE,
    check_rate_limit,
    current_principal,
    require_configured_secret,
)
from app.audit.trace import (
    record,
    trace_by_correlation,
    trace_for,
    trace_for_incident,
)
from app.config import get_settings, set_runtime_llm_provider
from app.db import session_scope
from app.detection import detect
from app.detection.engine import open_incidents
from app.eval.runner import load_scenarios, run_scenario
from app.failures import TAXONOMY, describe
from app.incidents.lifecycle import legal_from
from app.incidents.manager import investigate
from app.metrics import objectives, operational_metrics
from app.models import (
    AgentAction,
    AgentMessage,
    AgentTask,
    Approval,
    Incident,
    IncidentEvidence,
    RecoveryCandidate,
    RecoveryPlan,
    ToolCall,
    VerificationState,
    WebhookEvent,
    WebhookStatus,
    utcnow,
)
from app.notify import channels as notify_channels
from app.notify import consumers as notify
from app.notify import service as notify_service
from app.observability import runtime_metrics as runtime
from app.observability.logs import configure_logging
from app.observability.middleware import ObservabilityMiddleware
from app.recovery import plan_recovery
from app.recovery.dispatch import (
    RecoveryStopped,
    dispatch_candidate,
    settle_plan,
)
from app.recovery.ledger import build_ledger, dashboard
from app.verification.reconciler import escalated_actions, reconcile
from app.webhooks import ingest

# Before the app, so anything logged during construction is already formatted
# and nothing has to remember to call it. Idempotent.
configure_logging()

# At import, before a single route is registered, and deliberately not inside a
# startup hook: a deployment that cannot sign tokens safely should fail to build
# the application at all rather than come up and serve one request with a
# forgeable identity. Raises only where a platform marker says this is a
# deployment, so a fresh clone still runs `make api` with no configuration.
require_configured_secret()

# Also at import, and for a related reason: a consumer registered lazily on
# first drain is a consumer that is absent for every event drained before the
# first drain. `register()` is idempotent -- `subscribe` appends, so calling it
# twice would deliver every notification twice.
#
# This validates `NOTIFY_CHANNELS` as a side effect. A deployment that lists
# `email` and has no SMTP host fails here rather than discovering it the first
# time an approval needs a human.
notify.register()
notify_service.check_configuration()

app = FastAPI(title="MerchantOps Agent", version="0.1.0")

# Outermost, so it sees the status an exception handler eventually produced and
# times the handlers too. A metric that excludes error handling is a metric that
# looks best exactly when the service is worst.
app.add_middleware(ObservabilityMiddleware)


@app.exception_handler(AgentRuntimeError)
def agent_runtime_error(request: Request, exc: AgentRuntimeError):
    """A run died on something nobody classified.

    Registered once rather than caught per route: every endpoint that starts a
    run — a task, an incident investigation, a dispatched candidate, a replay —
    fails the same way, and a handler each would be four chances to forget one.

    The response carries the task id and nothing else about the error. The
    detail is already in the audit trail, where it is redacted and access is
    controlled; putting it in an HTTP body would publish an internal stack
    detail to whoever sent the request. 500 is honest: this is our defect, and
    §56 classifies INTERNAL_ERROR as ESCALATE — not something to retry.
    """
    return JSONResponse(status_code=500, content={"detail": {
        "error": "The run stopped on an internal error.",
        "code": "INTERNAL_ERROR",
        "task_id": exc.task_id,
        # False means the trace could not be written either, so pointing an
        # operator at the task id would send them to a page that is not there.
        "trace_preserved": exc.persisted,
        "failure": describe("INTERNAL_ERROR"),
    }})


# Authentication and rate limiting live in app/api/security.py. The caller
# presents a signed bearer token it cannot forge; permissions are still read
# from the database on every request, never carried in the token (CONTRACT §11).


class TaskRequest(BaseModel):
    request: str


class ProviderRequest(BaseModel):
    provider: str


def principal_tenant(s, merchant_id: str) -> str | None:
    """The tenant a merchant belongs to. Read, never taken from a request."""
    return s.execute(text("SELECT tenant_id FROM merchants WHERE id = :m"),
                     {"m": merchant_id}).scalar()


def _task_view(s, task: AgentTask) -> dict:
    approvals = s.query(Approval).filter(Approval.task_id == task.id).all()
    actions = s.query(AgentAction).filter(AgentAction.task_id == task.id).all()
    return {
        "id": task.id, "tenant_id": principal_tenant(s, task.merchant_id),
        "merchant_id": task.merchant_id, "user_id": task.user_id,
        "request": task.request, "status": task.status.value,
        "final_answer": task.final_answer, "failure_code": task.failure_code,
        "findings": task.findings, "tool_calls": task.tool_call_count,
        # MerchantOps §37. `confidence` is displayed and consulted by nothing.
        "intent": task.intent,
        "recommendation": task.recommendation,
        "agent_confidence": task.agent_confidence,
        # The model may RAISE this and never lower it, so it is an OR and not a
        # field the model owns. A pending approval means a human is required
        # whatever the model said about it.
        "requires_human": bool(approvals) or task.model_requires_human,
        "model_requires_human": task.model_requires_human,
        "llm_turns": task.llm_turn_count, "duration_ms": task.duration_ms,
        # MerchantOps §41 — everything needed to reproduce this run.
        "versions": {
            "agent": task.agent_version,
            "model_provider": task.model_provider,
            "model": task.model_version,
            "prompt": task.prompt_version,
            "tool_registry": task.tool_registry_version,
            "policy": task.policy_version,
            "workflow": task.workflow_version,
        },
        "agent_version": task.agent_version, "model_version": task.model_version,
        "prompt_version": task.prompt_version,
        # MerchantOps §56 — category, retryability, owner and what to do next.
        # A failure code alone tells an operator what broke and not whether
        # trying again is sensible, which is the question they actually have.
        "failure": describe(task.failure_code),
        "is_replay": task.is_replay, "replayed_from": task.replayed_from,
        "approvals": [{
            "id": a.id, "decision": a.decision, "action_type": a.action_type,
            "action_payload": a.action_payload, "risk_level": a.risk_level,
            "expires_at": a.expires_at.isoformat(), "decided_by": a.decided_by,
            # MerchantOps §26. The UI renders how many more people must sign;
            # it never decides. `signed_by` is what makes "a different person"
            # visible to the second approver before they click.
            "required_signatures": a.required_signatures,
            "signed_by": [s.user_id for s in a.signatures if s.decision == "APPROVED"],
        } for a in approvals],
        "actions": [{
            "id": a.id, "action_type": a.action_type, "status": a.status.value,
            "target_payment_id": a.target_payment_id,
            "external_payment_id": a.external_payment_id,
            "amount_minor": a.amount_minor, "external_reference": a.external_reference,
            "verification_state": a.verification_state.value if a.verification_state else None,
            "verification_detail": a.verification_detail,
            "verify_attempts": a.verify_attempts,
        } for a in actions],
    }


def _owned(s, task_id: str, principal: Principal) -> AgentTask:
    task = s.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "Unknown task.")
    if task.merchant_id != principal.merchant_id:
        # Do not leak existence across merchants.
        raise HTTPException(404, "Unknown task.")
    return task


@app.get("/health", response_model=schemas.Health,
          response_model_exclude_unset=True)
def health():
    s = get_settings()
    return {
        "status": "ok",
        "llm_provider": s.resolved_llm_provider,
        # The source, not the credential. Without it a `deterministic` reading
        # is ambiguous: deliberately configured, or no credentials found?
        "llm_credential_source": s.anthropic_credential_source,
        "llm_provider_is_explicit": s.llm_provider != "auto",
        # `runtime` means someone switched it in this process; it does not
        # survive a restart, and a published metric was not measured under it
        # unless the report says so.
        "llm_provider_source": s.llm_provider_source,
        "llm_model": s.llm_model if s.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        "payment_adapter": s.resolved_razorpay_mode,
        "razorpay_execution_is_real": s.resolved_razorpay_mode == "live_test_mode",
        "auth": "bearer_hmac",
        "auth_secret_is_development_default": DEV_SECRET_IN_USE,
        # Without a secret the webhook endpoint stores deliveries but refuses to
        # act on them. Published so "nothing happened" is never ambiguous
        # between "no events" and "events arrived unverified".
        "webhook_signature_verification": s.webhook_verification_enabled,
        # Both numbers, because they disagreed once and nothing said so. A
        # budget above the host's own timeout is not enforced by us — the
        # invocation is killed part-way and the ABORTED_BUDGET path never runs —
        # so what is actually enforced belongs where the posture is read.
        "agent_budget": {
            "configured_wall_clock_seconds": s.max_wall_clock_seconds,
            "platform_timeout_seconds": s.platform_timeout_seconds,
            "enforced_wall_clock_seconds": s.effective_wall_clock_seconds,
            "capped_by_platform": s.effective_wall_clock_seconds < s.max_wall_clock_seconds,
            "max_tool_calls": s.max_tool_calls_per_task,
            "max_llm_turns": s.max_llm_turns_per_task,
        },
        # Reported for the same reason the token secret and the budget are: a
        # posture that differs from the one somebody assumes is the failure this
        # endpoint exists to prevent. Three API processes with no Redis serve
        # three times the configured rate limit, and every other field here
        # would look correct.
        "shared_state": {
            "backend": shared_state.backend(),
            "rate_limit_scope": _scope_word(),
            "provider_override_scope": _scope_word(),
        },
        "agent_execution_mode": s.agent_execution_mode,
        # Reported here rather than only on /ready, because a dead worker is not
        # a reason to take this instance out of rotation -- the API serves reads
        # and inline runs perfectly well without one. It is a reason to page
        # somebody, and this is where somebody looks.
        "queue": _queue_state(),
    }


def _queue_state() -> dict:
    from app.agent.queue import queue_state

    try:
        with session_scope() as s:
            return queue_state(s).as_dict()
    except Exception:
        # /health answers without touching anything by design, and adding a
        # query must not change that contract. An unreachable database is
        # /ready's question, not this one.
        return {"queued": 0, "running": 0, "oldest_queued_seconds": None,
                "worker_seen_seconds_ago": None, "worker_is_live": False}


def _scope_word() -> str:
    return "all_replicas" if shared_state.backend() == "shared" else "this_replica_only"


@app.get("/ready", response_model=schemas.Readiness,
         response_model_exclude_unset=True)
def ready(response: Response):
    """Readiness, which is a different question from `/health`.

    `/health` reports the process's posture -- which provider, which adapter,
    what budget -- and answers without touching anything. That makes it a
    liveness probe: if it returns, the process is alive and should not be
    restarted.

    It is the wrong probe to route traffic on. A container whose database is
    unreachable, or whose schema is behind the code deployed over it, is alive
    and cannot serve a request. Sending it traffic produces 500s that look like
    an application fault.

    So this one asks the two questions that decide whether this instance can do
    work: can it reach the database, and is the schema at the revision this code
    expects. Returns 503 when either fails, because an orchestrator reads the
    status code and not the body.

    Deliberately unauthenticated, like `/health`: a probe cannot hold a bearer
    token, and what it discloses is whether the service works -- which anyone
    who can reach it discovers by sending a request anyway. It reports no
    configuration and no data.
    """
    checks: dict[str, dict] = {}
    ok = True

    at = None
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:
        ok = False
        # The type, not the message. A connection error carries the DSN, and a
        # probe endpoint is the last place to publish credentials.
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    if checks["database"]["ok"]:
        # A separate transaction, and a separate failure. Asking both questions
        # in one `try` made an unmigrated database report as an unreachable one:
        # `alembic_version` not existing raises, and the handler above blamed
        # the connection. Two different causes with two different remedies --
        # one is "the database is down", the other is "run the migrations" --
        # and a probe that cannot tell them apart sends whoever is paged to the
        # wrong place.
        try:
            with session_scope() as s:
                at = s.execute(
                    text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            at = None

    if at is not None:
        from scripts.migrate import head_revision

        expected = head_revision()
        matches = at == expected
        ok = ok and matches
        checks["schema"] = {"ok": matches, "at": at, "expected": expected}
    elif checks["database"]["ok"]:
        # Reachable, and carrying no version row at all. That is a database
        # nothing has migrated -- not a transient failure, and not ready.
        ok = False
        checks["schema"] = {"ok": False, "at": None,
                            "expected": None, "error": "unstamped"}

    if not ok:
        response.status_code = 503
    return {"ready": ok, "checks": checks}


@app.get("/me", response_model=schemas.Me,
          response_model_exclude_unset=True)
def whoami(principal: Principal = Depends(current_principal)):
    """Who the caller is, as the server understands it.

    The client never asserts this. It is returned so an interface can show the
    acting identity — the same screen behaves differently for an owner and an
    analyst, and an operator should not have to infer which one they are.
    """
    return {"tenant_id": principal.tenant_id, "user_id": principal.user_id,
            "merchant_id": principal.merchant_id,
            "role": principal.role, "permissions": principal.permissions}


@app.post("/config/llm-provider", response_model=schemas.ProviderChange,
          response_model_exclude_unset=True)
def set_llm_provider(body: ProviderRequest,
                     principal: Principal = Depends(current_principal)):
    """Switch between providers that are already configured.

    Deliberately narrow. It selects among providers the process can already
    reach; it never accepts a credential. CONTRACT §37 keeps secrets in the
    environment, and a browser form is not that.

    The override does not survive a restart, deliberately: persisting it would
    make the active provider a piece of durable state that no environment
    variable explains, which is a worse trade.

    Where it applies depends on the deployment, and the response says which.
    With `REDIS_URL` set it is shared, so the switch applies to every replica.
    Without it the override is process-local and this switched the provider for
    whichever replica happened to serve the request -- an operator switching to
    the deterministic planner would then watch the model keep being used by the
    others. `applies_to` is `fleet` or `this_replica_only`, because those are
    different outcomes and the operator is entitled to know which one happened.
    """
    s = get_settings()
    choice = body.provider.lower()
    if choice not in {"auto", "deterministic", "anthropic"}:
        raise HTTPException(422, {"error": f"Unknown provider '{body.provider}'.",
                                  "code": "unknown_provider"})
    if principal.role != "owner":
        # 403 rather than 404: this is not a merchant-scoped resource, so
        # refusing plainly leaks nothing.
        raise HTTPException(403, {"error": "Changing the provider requires the owner role.",
                                  "code": "role_required"})
    if choice == "anthropic" and not s.anthropic_credential_source:
        raise HTTPException(409, {
            "error": "No Anthropic credential is configured on the server. "
                     "Set ANTHROPIC_API_KEY or sign in with `ant auth login`, then retry.",
            "code": "no_credential"})

    before = s.resolved_llm_provider
    shared = set_runtime_llm_provider(None if choice == "auto" else choice)
    after = s.resolved_llm_provider

    with session_scope() as sess:
        # Audited like any other privileged change. No task owns this event,
        # which is why audit_logs.task_id is nullable.
        record(sess, SimpleNamespace(id=None, merchant_id=principal.merchant_id,
                                     user_id=principal.user_id),
               "llm_provider_changed",
               {"from": before, "to": after, "requested": choice,
                "by": principal.user_id, "role": principal.role,
                # Audited, because "the provider was changed" and "the provider
                # was changed on one of three replicas" are different events and
                # only one of them explains the metrics afterwards.
                "applies_to": "fleet" if shared else "this_replica_only"})

    return {"llm_provider": after, "llm_provider_source": s.llm_provider_source,
            "llm_model": s.llm_model if after == "anthropic" else "deterministic-planner-v1",
            "changed_from": before,
            "applies_to": "fleet" if shared else "this_replica_only"}


@app.post("/tasks", response_model=schemas.TaskView,
          response_model_exclude_unset=True, status_code=200)
def create_task(body: TaskRequest, response: Response, mode: str | None = None,
                principal: Principal = Depends(current_principal)):
    """Start a task, inline or queued.

    `inline` runs the loop inside this request and returns the finished task,
    which is what this route has always done. It is the only thing possible
    where there is no worker -- Vercel, or a bare `make api` -- and it is what
    the evaluation suite exercises (through `AgentRuntime` directly, so none of
    this affects the scenario numbers).

    `async` writes the task, returns **202** with its id, and lets a worker run
    it. That is what removes the request timeout from the design: an
    investigation can take as long as its budget allows without a proxy giving
    up, and the API process is free while it does.

    The mode comes from `AGENT_EXECUTION_MODE` and `?mode=` overrides it. Either
    way the response is a `TaskView`; a queued one is simply `QUEUED` with no
    answer yet, and `GET /tasks/{id}` is the poll.

    **An asynchronous submission is refused when no worker has been seen
    recently.** Accepting it would return 202 for a task that will never start,
    and a queue nobody is draining looks exactly like a queue with nothing in
    it. 503 says which of those it is.
    """
    choice = (mode or get_settings().agent_execution_mode).lower()
    if choice not in {"inline", "async"}:
        raise HTTPException(422, {
            "error": f"Unknown execution mode '{choice}'. Use 'inline' or 'async'.",
            "code": "unknown_mode"})

    if choice == "inline":
        with session_scope() as s:
            out = AgentRuntime(s, principal).run(body.request)
            return _task_view(s, out.task)

    from app.agent.queue import enqueue, queue_state

    with session_scope() as s:
        state = queue_state(s)
        if not state.worker_is_live:
            raise HTTPException(503, {
                "error": "No worker has reported in recently, so a queued task "
                         "would not start. Run `make worker` (or the worker "
                         "container), or submit with ?mode=inline.",
                "code": "no_worker",
                "worker_seen_seconds_ago": state.worker_seen_seconds_ago,
                "queued": state.queued})

        task = enqueue(s, principal, body.request)
        response.status_code = 202
        return _task_view(s, task)


@app.get("/tasks/{task_id}", response_model=schemas.TaskView,
          response_model_exclude_unset=True)
def get_task(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        return _task_view(s, _owned(s, task_id, principal))


@app.get("/tasks/{task_id}/trace", response_model=schemas.TaskTrace,
          response_model_exclude_unset=True)
def get_trace(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        return {"task_id": task_id, "trace": trace_for(s, task_id)}


@app.get("/tasks/{task_id}/evidence", response_model=schemas.TaskEvidence,
          response_model_exclude_unset=True)
def get_evidence(task_id: str, principal: Principal = Depends(current_principal)):
    """The evidence behind a task's findings and its pending action.

    CONTRACT §21 says the human reviews payment, amount, reason, **evidence**
    and risk before approving. The Streamlit UI reads `tool_calls` straight from
    the database to do that; an HTTP client had no route to the same facts, so a
    non-Streamlit approval screen could only show four of the five.

    `untrusted` is carried through deliberately (CONTRACT §36). Merchant and
    customer free text is an injection surface, and a client needs to know which
    values to render as quarantined data rather than as system text. Stripping
    the flag here would push that judgement onto every consumer.
    """
    with session_scope() as s:
        _owned(s, task_id, principal)
        rows = s.query(ToolCall).filter(ToolCall.task_id == task_id) \
            .order_by(ToolCall.seq).all()
        return {
            "task_id": task_id,
            "tool_calls": [{
                "id": c.id, "seq": c.seq, "tool": c.tool_name,
                "arguments": c.input, "success": c.success,
                "error_code": c.error_code, "risk_level": c.risk_level,
                "policy_decision": c.policy_decision, "duration_ms": c.duration_ms,
                "evidence": (c.output or {}).get("evidence", []),
                "data": (c.output or {}).get("data", {}),
            } for c in rows],
        }


@app.get("/approvals", response_model=schemas.ApprovalQueue,
          response_model_exclude_unset=True)
def list_approvals(pending_only: bool = True,
                   principal: Principal = Depends(current_principal)):
    """The approval queue — MerchantOps §65.

    Reachable before only through the task that owned it, which meant an
    operator wanting "what is waiting on me" had to assemble one client-side.
    An approval is a thing a person acts on, so it is a resource.
    """
    with session_scope() as s:
        q = s.query(Approval).filter(Approval.merchant_id == principal.merchant_id)
        if pending_only:
            q = q.filter(Approval.decision == "PENDING")
        rows = q.order_by(Approval.created_at).all()
        now = utcnow()
        return {"approvals": [{
            "id": a.id, "task_id": a.task_id, "action_type": a.action_type,
            "action_payload": a.action_payload, "risk_level": a.risk_level,
            "decision": a.decision, "decided_by": a.decided_by,
            "required_signatures": a.required_signatures,
            "signed_by": [x.user_id for x in a.signatures if x.decision == "APPROVED"],
            "created_at": a.created_at.isoformat(),
            "expires_at": a.expires_at.isoformat(),
            # An expired approval is still PENDING in the database until someone
            # tries to use it. The queue says so rather than showing it as work
            # an operator can still do.
            "expired": a.expires_at.replace(tzinfo=now.tzinfo) < now
                       if a.expires_at.tzinfo is None else a.expires_at < now,
        } for a in rows]}


# Declared before /actions/{action_id}: Starlette matches in declaration order,
# so with the parametrised route first "escalated" is captured as an action_id
# and the endpoint 404s as "Unknown action."
@app.get("/actions/escalated", response_model=list[schemas.EscalatedAction],
          response_model_exclude_unset=True)
def list_escalated(max_attempts: int = 5,
                   principal: Principal = Depends(current_principal)):
    """Actions automatic reconciliation could not settle — the operator queue."""
    with session_scope() as s:
        rows = escalated_actions(s, max_attempts=max_attempts)
    # Merchant isolation applies here too.
    return [r for r in rows if r["merchant_id"] == principal.merchant_id]


@app.get("/actions/{action_id}", response_model=schemas.ActionDetail,
          response_model_exclude_unset=True)
def get_action(action_id: str, principal: Principal = Depends(current_principal)):
    """One external action — MerchantOps §65.

    Everything a person needs to judge whether money moved: the verification
    state and its detail, the idempotency key it was claimed under, the
    provider reference, and how long each half took.
    """
    with session_scope() as s:
        a = s.get(AgentAction, action_id)
        if a is None or a.merchant_id != principal.merchant_id:
            raise HTTPException(404, "Unknown action.")
        return {
            "id": a.id, "task_id": a.task_id, "action_type": a.action_type,
            "target_payment_id": a.target_payment_id,
            "external_payment_id": a.external_payment_id,
            "amount_minor": a.amount_minor, "status": a.status.value,
            "verification_state": a.verification_state.value if a.verification_state else None,
            "verification_detail": a.verification_detail,
            "verify_attempts": a.verify_attempts,
            "external_reference": a.external_reference,
            "approval_id": a.approval_id,
            "recovery_candidate_id": a.recovery_candidate_id,
            # Truncated: the key is derived from server-held facts and proves
            # nothing useful in full, while printing it in full puts a
            # deduplication token into logs and screenshots.
            "idempotency_key_prefix": a.idempotency_key[:16] + "...",
            "provider_latency_ms": a.provider_latency_ms,
            "verification_latency_ms": a.verification_latency_ms,
            "created_at": a.created_at.isoformat(),
            "updated_at": a.updated_at.isoformat(),
        }


@app.get("/tasks/{task_id}/messages", response_model=schemas.TaskMessages,
          response_model_exclude_unset=True)
def get_messages(task_id: str, principal: Principal = Depends(current_principal)):
    """The conversation the model actually saw — MerchantOps §66, §38.

    Distinct from the trace, which records what the application DID. This is
    what the model was looking at when it decided to do it, and it is the only
    way to answer "why did it call that tool" without reconstructing an answer
    from the outside.

    `contains_untrusted` is carried through deliberately (§39). Merchant free
    text was quarantined when the model saw it, and a client rendering a stored
    transcript needs to know which messages to render as data rather than as
    system text.
    """
    with session_scope() as s:
        _owned(s, task_id, principal)
        rows = (s.query(AgentMessage).filter(AgentMessage.task_id == task_id)
                .order_by(AgentMessage.seq).all())
        return {
            "task_id": task_id,
            "messages": [{
                "seq": m.seq, "turn": m.turn, "role": m.role,
                "content": m.content, "contains_untrusted": m.contains_untrusted,
                "char_count": m.char_count, "at": m.created_at.isoformat(),
            } for m in rows],
            "total_chars": sum(m.char_count for m in rows),
        }


@app.post("/tasks/{task_id}/approve", response_model=schemas.TaskView,
          response_model_exclude_unset=True)
def approve(task_id: str, principal: Principal = Depends(current_principal)):
    """Record this caller's approval, and execute once enough people have signed.

    For a CRITICAL action policy demands two signatures from two different
    people, so a single call here returns with `awaiting_signatures` set and
    nothing external touched. Signing twice yourself is refused by a database
    constraint, not by this handler.
    """
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            r = approve_and_execute(s, task_id, principal)
        except ApprovalError as e:
            # `from e` keeps the ApprovalError in the traceback. Without it the
            # log shows only the 409 and the reason it was raised is gone --
            # which for an approval refusal is the whole story.
            raise HTTPException(409, {"error": str(e), "code": e.code}) from e
        view = _task_view(s, r["task"])
        if r.get("awaiting_signatures"):
            view["awaiting_signatures"] = r["awaiting_signatures"]
            view["signed_by"] = r["signatures"]
        return view


@app.post("/tasks/{task_id}/reject", response_model=schemas.TaskView,
          response_model_exclude_unset=True)
def reject_task(task_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            task = reject(s, task_id, principal)
        except ApprovalError as e:
            # `from e` keeps the ApprovalError in the traceback. Without it the
            # log shows only the 409 and the reason it was raised is gone --
            # which for an approval refusal is the whole story.
            raise HTTPException(409, {"error": str(e), "code": e.code}) from e
        return _task_view(s, task)


@app.post("/tasks/{task_id}/reverify", response_model=schemas.ReverifyResult,
          response_model_exclude_unset=True)
def reverify_task(task_id: str, principal: Principal = Depends(current_principal)):
    """CONTRACT §26 (amended) — the UNKNOWN exit path."""
    with session_scope() as s:
        _owned(s, task_id, principal)
        try:
            r = reverify(s, task_id, principal)
        except ApprovalError as e:
            # `from e` keeps the ApprovalError in the traceback. Without it the
            # log shows only the 409 and the reason it was raised is gone --
            # which for an approval refusal is the whole story.
            raise HTTPException(409, {"error": str(e), "code": e.code}) from e
        return {"task": _task_view(s, r["task"]),
                "verification": r["verification"].as_dict()}


@app.post("/tasks/{task_id}/replay", response_model=schemas.ReplayResult,
          response_model_exclude_unset=True)
def replay_task(task_id: str, mode: str = "PLAYBACK",
                principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        _owned(s, task_id, principal)
        if mode.upper() == "RE_REASON":
            return re_reason(s, task_id, principal)
        return playback(s, task_id)


@app.post("/actions/reconcile", response_model=schemas.ReconcileReport,
          response_model_exclude_unset=True)
def run_reconcile(min_age_seconds: int = 30, max_attempts: int = 5,
                  principal: Principal = Depends(current_principal)):
    """Settle actions left UNKNOWN/PARTIAL. Re-reads state only; never retries
    the financial action."""
    with session_scope() as s:
        rep = reconcile(s, min_age_seconds=min_age_seconds, max_attempts=max_attempts)
        return rep.as_dict()


# --------------------------------------------------------------------------
# Recovery — MerchantOps §23, §27, §28, §65
# --------------------------------------------------------------------------
def _plan_view(s, plan: RecoveryPlan, *, detail: bool = False) -> dict:
    view = {
        "id": plan.id, "incident_id": plan.incident_id,
        "merchant_id": plan.merchant_id, "status": plan.status.value,
        "intervention": plan.intervention.value,
        "revenue_at_risk_minor": plan.revenue_at_risk_minor,
        "eligible_recovery_minor": plan.eligible_recovery_minor,
        # Expected is an ESTIMATE. It is returned next to its basis so a client
        # cannot render the figure without the reasoning, and §49 keeps it in a
        # different field from anything actually recovered.
        "expected_recovery_minor": plan.expected_recovery_minor,
        "expected_recovery_basis": plan.expected_recovery_basis,
        "budget": {
            "max_recovery_minor": plan.max_recovery_minor,
            "max_actions": plan.max_actions,
            "max_attempts_per_customer": plan.max_attempts_per_customer,
            "max_duration_seconds": plan.max_duration_seconds,
            # v2 §38's fifth bound. Shown here too, so a reader of the plan
            # sees the same five limits the campaign card shows.
            "max_risk_level": plan.max_risk_level,
        },
        "stop_rule": plan.stop_rule, "stop_reason": plan.stop_reason,
        "planner_version": plan.planner_version,
        "expires_at": plan.expires_at.isoformat(),
    }
    if not detail:
        return view
    view["candidates"] = [{
        "id": c.id, "rank": c.rank, "payment_id": c.payment_id,
        "customer_id": c.customer_id, "amount_minor": c.amount_minor,
        "intervention": c.intervention.value, "status": c.status.value,
        "ineligible_reason": c.ineligible_reason,
        "expected_recovery_minor": c.expected_recovery_minor,
        "actual_recovery_minor": c.actual_recovery_minor,
        "executable": c.executable, "attempts": c.attempts, "task_id": c.task_id,
    } for c in s.query(RecoveryCandidate)
        .filter(RecoveryCandidate.plan_id == plan.id)
        .order_by(RecoveryCandidate.rank).all()]
    return view


def _owned_plan(s, plan_id: str, principal: Principal) -> RecoveryPlan:
    plan = s.get(RecoveryPlan, plan_id)
    if plan is None or plan.merchant_id != principal.merchant_id:
        raise HTTPException(404, "Unknown recovery plan.")
    return plan


@app.post("/incidents/{incident_id}/recovery", response_model=schemas.PlanView,
          response_model_exclude_unset=True)
def create_recovery_plan(incident_id: str,
                         principal: Principal = Depends(current_principal)):
    """Plan recovery for an incident — MerchantOps §23.

    Planning does not execute. It computes affected transactions, eligibility
    and expected recovery, and bounds the campaign; acting on a candidate is a
    separate, individually gated call.
    """
    with session_scope() as s:
        inc = _owned_incident(s, incident_id, principal)
        r = plan_recovery(s, inc, principal=principal)
        view = _plan_view(s, r.plan, detail=True)
        view["created"] = r.created
        return view


@app.get("/recovery/plans/{plan_id}", response_model=schemas.PlanView,
          response_model_exclude_unset=True)
def get_recovery_plan(plan_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        return _plan_view(s, _owned_plan(s, plan_id, principal), detail=True)


@app.get("/state", response_model=schemas.MerchantStateView,
         response_model_exclude_unset=True)
def get_merchant_state(principal: Principal = Depends(current_principal)):
    """The merchant digital twin — MerchantOps v2 §14.

    One coherent view of operational health, assembled from the modules that
    already own each figure rather than recomputed here. Computed per read: the
    numbers derive from rows that change underneath them, and a cached count
    disagrees with its rows the first time a candidate moves (ADR-0040).

    `/recovery/ledger` and the dashboard remain: this calls them. A branch §14
    names that nothing measures reports `measured: false` with a reason instead
    of a zero.
    """
    from app.state import build_state

    with session_scope() as s:
        return build_state(s, principal.merchant_id).as_dict()


@app.get("/campaigns", response_model=schemas.CampaignList,
         response_model_exclude_unset=True)
def list_campaigns(principal: Principal = Depends(current_principal)):
    """Recovery campaigns still capable of acting — MerchantOps v2 §37.

    A campaign IS a recovery plan; there is no second table. This is §37's card
    over the same rows, with the affected/eligible split and the budget
    consumption the plan does not store because both go stale the moment a
    candidate moves.

    Ordered by expected recovery, so the campaign worth watching is first.
    Finished campaigns are excluded for the same reason `open_incidents`
    excludes RESOLVED.
    """
    from app.recovery.campaign import active_campaigns, summary

    with session_scope() as s:
        cards = [summary(s, p) for p in active_campaigns(s, principal.merchant_id)]
        return {
            "campaigns": cards,
            "total_expected_recovery_minor": sum(
                c["expected_recovery_minor"] for c in cards),
        }


@app.get("/campaigns/{plan_id}", response_model=schemas.CampaignView,
         response_model_exclude_unset=True)
def get_campaign(plan_id: str, principal: Principal = Depends(current_principal)):
    """§37's card for one campaign, finished or not.

    Unlike the list, this does not exclude a stopped campaign: the question
    "what happened to RC-017" is asked most often about one that ended.
    """
    from app.recovery.campaign import summary

    with session_scope() as s:
        return summary(s, _owned_plan(s, plan_id, principal))


@app.post("/recovery/plans/{plan_id}/settle", response_model=schemas.SettleReport,
          response_model_exclude_unset=True)
def settle_recovery_plan(plan_id: str,
                         principal: Principal = Depends(current_principal)):
    """Read each dispatched candidate's outcome back from its verified action."""
    with session_scope() as s:
        return settle_plan(s, _owned_plan(s, plan_id, principal))


@app.post("/recovery/candidates/{candidate_id}/dispatch", response_model=schemas.DispatchResult,
          response_model_exclude_unset=True)
def dispatch_recovery_candidate(candidate_id: str,
                                principal: Principal = Depends(current_principal)):
    """Hand one candidate to the ordinary agent path, bounds permitting.

    Returns 409 with the stopping rule that fired when §27's budget or §28's
    rules refuse. That is a normal outcome for a bounded campaign, not an error
    condition — the rule name is the answer.
    """
    with session_scope() as s:
        cand = s.get(RecoveryCandidate, candidate_id)
        if cand is None or cand.merchant_id != principal.merchant_id:
            raise HTTPException(404, "Unknown recovery candidate.")
        plan = _owned_plan(s, cand.plan_id, principal)
        try:
            r = dispatch_candidate(s, plan, cand, principal)
        except RecoveryStopped as e:
            # RETURNED, not raised. `session_scope` rolls back on an exception,
            # and the stop it would roll back is the record that this campaign
            # halted -- leaving the plan DRAFT and the bound free to be tried
            # again. A stop that does not survive the response is exactly the
            # "recorded but not applied" failure MerchantOps §28 forbids.
            return JSONResponse(status_code=409, content={
                "detail": {"error": e.decision.reason,
                           "code": "RECOVERY_STOPPED",
                           "stop": e.decision.as_dict(),
                           "plan": _plan_view(s, plan)}})
        return {"candidate_id": cand.id, "task": _task_view(s, r["task"]),
                "risk": r["risk"].as_dict(),
                "plan": _plan_view(s, plan)}


# --------------------------------------------------------------------------
# Provider webhooks — MerchantOps §34, §65
# --------------------------------------------------------------------------
@app.post("/webhooks/razorpay", response_model=schemas.WebhookAck,
          response_model_exclude_unset=True)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
):
    """Ingest one provider delivery.

    Deliberately outside `current_principal`: the provider holds no bearer token,
    so the HMAC signature is the authentication, not a token. This is a distinct
    trust path and it is the only unauthenticated write in the application.

    Always returns 200 once the delivery is stored, including for a signature
    that failed. A provider that receives a non-2xx retries, and retrying a
    forged or malformed delivery achieves nothing except load — the outcome is
    in the body, and in `webhook_events`.
    """
    # The RAW bytes. The signature covers exactly what was sent; re-serialising
    # parsed JSON changes key order and whitespace, and the check would never
    # pass again.
    raw = await request.body()

    # Rate limited on the endpoint rather than on an identity, because there is
    # no authenticated identity here to limit.
    check_rate_limit("webhook:razorpay", request.url.path, "POST")

    with session_scope() as s:
        return ingest(s, raw, x_razorpay_signature, x_razorpay_event_id).as_dict()


@app.get("/webhooks/events", response_model=schemas.WebhookEventList,
          response_model_exclude_unset=True)
def list_webhook_events(limit: int = 50, status: str | None = None,
                        principal: Principal = Depends(current_principal)):
    """The event store, scoped to the caller's merchant.

    Events we could not attribute to a merchant — an unknown entity, or a
    delivery that failed its signature and was therefore never resolved — are
    visible only in aggregate. Showing their bodies to whoever asks first would
    make an unauthenticated endpoint into a cross-tenant read.
    """
    with session_scope() as s:
        q = s.query(WebhookEvent).filter(WebhookEvent.merchant_id == principal.merchant_id)
        if status:
            q = q.filter(WebhookEvent.status == WebhookStatus(status.upper()))
        rows = q.order_by(WebhookEvent.received_at.desc()).limit(min(limit, 200)).all()

        unattributed = s.query(WebhookEvent).filter(
            WebhookEvent.merchant_id.is_(None)).count()
        return {
            "events": [{
                "id": e.id, "event_id": e.event_id, "event_type": e.event_type,
                "status": e.status.value, "signature_valid": e.signature_valid,
                "entity_id": e.entity_id, "correlation_id": e.correlation_id,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "received_at": e.received_at.isoformat(),
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
                "note": e.processing_note,
            } for e in rows],
            "unattributed_count": unattributed,
        }


# --------------------------------------------------------------------------
# Live events — MerchantOps v2 §62, §65
# --------------------------------------------------------------------------
@app.get("/events", response_model=schemas.LiveEventList,
         response_model_exclude_unset=True)
def list_events(after: str | None = None, limit: int = 100,
                principal: Principal = Depends(current_principal)):
    """The event stream as a cursor-paged read — MerchantOps v2 §62.

    This is the endpoint the UI timeline actually runs on, and `/events/stream`
    is a convenience layered over it. Polling a cursor is unglamorous and it is
    also the only thing that works on the deployment target: a Vercel function
    has a wall-clock limit, so a held-open SSE connection is a connection that
    drops on a timer and reconnects, which is a poll with extra steps and worse
    failure modes.

    Scoped to the caller's merchant. `pending` is reported alongside because a
    drain that has stopped is invisible from the frames themselves — the
    timeline simply stops moving, which looks like a quiet system.
    """
    from app.events.bus import PostgresEventStore
    from app.models import EventOutbox, OutboxStatus

    with session_scope() as s:
        events = PostgresEventStore().since(
            s, after=after, merchant_id=principal.merchant_id,
            limit=min(limit, 500))
        pending = s.query(EventOutbox).filter(
            EventOutbox.merchant_id == principal.merchant_id,
            EventOutbox.status == OutboxStatus.PENDING).count()
        return {
            "events": [e.as_dict() for e in events],
            "next_cursor": events[-1].id if events else after,
            "pending": pending,
        }


@app.get("/events/stream")
def stream_events(after: str | None = None, seconds: int = 25,
                  principal: Principal = Depends(current_principal)):
    """The same events as `text/event-stream` — MerchantOps v2 §62, §65.

    Bounded on purpose. The connection closes after `seconds` and the browser's
    `EventSource` reconnects with `Last-Event-ID`, which is the cursor. An
    unbounded stream would hold a database connection for as long as a tab is
    open; on Vercel it would be killed anyway, and holding one per idle tab is
    how a connection pool is exhausted by users who are reading rather than
    doing anything.

    The merchant scope is resolved once, here, from the bearer token — never
    from a query parameter. A stream is still an authorised read.
    """
    import json as _json
    import time as _time

    from fastapi.responses import StreamingResponse

    from app.events.bus import PostgresEventStore

    merchant_id = principal.merchant_id
    store = PostgresEventStore()
    deadline = _time.monotonic() + max(1, min(seconds, 60))

    def frames():
        cursor = after
        # Tell the client how long we intend to stay, so a reconnect storm is
        # a deliberate cadence rather than a surprise.
        yield f"retry: 2000\n: window {int(deadline - _time.monotonic())}s\n\n"
        while _time.monotonic() < deadline:
            with session_scope() as s:
                batch = store.since(s, after=cursor, merchant_id=merchant_id,
                                    limit=200)
            for event in batch:
                cursor = event.id
                # `id:` is what the browser sends back as Last-Event-ID.
                yield (f"id: {event.id}\n"
                       f"event: {event.event_type}\n"
                       f"data: {_json.dumps(event.as_dict(), default=str)}\n\n")
            if not batch:
                # A comment frame, not an event: keeps proxies from closing an
                # idle connection without putting anything on the timeline.
                yield ": keep-alive\n\n"
                _time.sleep(1.0)

    return StreamingResponse(frames(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        # Nginx and several CDN edges buffer a response body by default, which
        # for an event stream means delivering it all at once at the end.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.post("/events/drain", response_model=schemas.DrainReport)
def drain_events(limit: int = 200, principal: Principal = Depends(current_principal)):
    """Deliver pending events to their in-process consumers.

    Exposed as a route because this deployment has no worker: v2 §13 permits a
    "managed queue/event mechanism", and on Vercel the available one is a
    scheduled invocation. Draining is idempotent and safe to call concurrently
    — the claim is `FOR UPDATE SKIP LOCKED` — so a cron that overlaps itself
    costs a wasted query rather than a double delivery.
    """
    from app.events.bus import drain

    with session_scope() as s:
        return drain(s, limit=min(limit, 1000))


@app.post("/notifications/sweep", response_model=schemas.NotifySweepReport)
def sweep_notifications(principal: Principal = Depends(current_principal)):
    """Send the notifications nothing raises an event for.

    An approval expiring is the absence of a decision and an escalated action is
    a threshold crossed, so neither has a moment to hook. Both are found by
    looking, on a cadence -- the same shape as detection and reconciliation, and
    the same honest limitation: bounded by how often this is called.

    Safe to call as often as you like. Every send is deduplicated by a UNIQUE
    constraint, so an overlapping cron costs queries and sends nothing twice.
    Until there is a scheduler this needs a caller; `scripts/notify_sweep.py` is
    the one for a cron.
    """
    from app.notify.sweep import sweep

    with session_scope() as s:
        return sweep(s)


@app.get("/notifications", response_model=schemas.NotificationList,
         response_model_exclude_unset=True)
def list_notifications(limit: int = 50,
                       principal: Principal = Depends(current_principal)):
    """What this merchant has been told, and what it has not.

    Merchant-scoped from the bearer token, never a query parameter. The point of
    reading it is `undelivered`: a notification recorded and never sent is
    somebody who was not told, and the only reason this table is queryable is so
    that state is findable rather than silent.
    """
    from app.models import NotificationStatus, OperatorNotification

    with session_scope() as s:
        rows = s.execute(
            select(OperatorNotification)
            .where(OperatorNotification.merchant_id == principal.merchant_id)
            .order_by(OperatorNotification.created_at.desc())
            .limit(min(limit, 200))
        ).scalars().all()
        undelivered = s.execute(
            select(func.count()).select_from(OperatorNotification).where(
                OperatorNotification.merchant_id == principal.merchant_id,
                OperatorNotification.status.in_([NotificationStatus.PENDING,
                                                 NotificationStatus.FAILED]))
        ).scalar() or 0
        return {
            "notifications": [{
                "id": n.id, "kind": n.kind.value, "severity": n.severity,
                "subject_type": n.subject_type, "subject_id": n.subject_id,
                "recipient": n.recipient, "channel": n.channel,
                "title": n.title, "status": n.status.value,
                "attempts": n.attempts, "last_error": n.last_error,
                "created_at": n.created_at.isoformat(),
                "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            } for n in rows],
            "undelivered": undelivered,
            "channels": notify_channels.active_channel_names(),
        }


# --------------------------------------------------------------------------
# Incidents — MerchantOps §13, §65
# --------------------------------------------------------------------------
def _incident_view(s, inc: Incident, *, detail: bool = False) -> dict:
    view = {
        "id": inc.id, "merchant_id": inc.merchant_id,
        "type": inc.incident_type.value, "severity": inc.severity.value,
        "status": inc.status.value, "title": inc.title, "summary": inc.summary,
        "revenue_at_risk_minor": inc.revenue_at_risk_minor,
        "detection_rule": inc.detection_rule,
        "detection_version": inc.detection_version,
        "correlation_id": inc.correlation_id,
        # MerchantOps v2 §33, §64. The band the platform computed, not the
        # number the model chose for itself. Null until the incident has been
        # investigated: an unassessed incident has no confidence, and showing
        # one would be asserting a view nobody formed.
        "confidence": inc.confidence_band,
        "started_at": inc.started_at.isoformat(),
        "detected_at": inc.detected_at.isoformat(),
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
    }
    if not detail:
        return view

    # The derivation, so "why HIGH?" has an answer that is not "the model said
    # so". This is the difference between a computed band and an opaque one.
    view["confidence_inputs"] = inc.confidence_inputs or {}
    view["signals"] = inc.signals
    view["evidence"] = [{
        "id": e.id, "key": e.key, "value": e.value.get("v"),
        "source": e.source, "untrusted": e.untrusted,
    } for e in s.query(IncidentEvidence)
        .filter(IncidentEvidence.incident_id == inc.id)
        .order_by(IncidentEvidence.id).all()]
    plan = (s.query(RecoveryPlan)
            .filter(RecoveryPlan.incident_id == inc.id).one_or_none())
    view["recovery"] = _plan_view(s, plan, detail=True) if plan else None
    # §51's timeline. Read from the audit trail, so it reports what the
    # application did rather than a narrative assembled beside it.
    view["timeline"] = [{
        "at": e["at"], "event": e["event"], "task_id": e["task_id"],
        "detail": {k: v for k, v in (e["payload"] or {}).items()
                   if k in ("from", "to", "reason", "decision", "rule",
                            "plan_id", "intervention", "state", "status")},
    } for e in trace_for_incident(s, inc.id)]
    view["tasks"] = [{
        "id": t.id, "status": t.status.value, "final_answer": t.final_answer,
        "tool_calls": t.tool_call_count, "duration_ms": t.duration_ms,
    } for t in s.query(AgentTask)
        .filter(AgentTask.incident_id == inc.id)
        .order_by(AgentTask.created_at).all()]
    # Which moves are available from here. The UI renders this; it never
    # computes it, for the same reason it never computes a policy outcome.
    view["legal_transitions"] = sorted(x.value for x in legal_from(inc.status))
    return view


def _owned_incident(s, incident_id: str, principal: Principal) -> Incident:
    inc = s.get(Incident, incident_id)
    if inc is None or inc.merchant_id != principal.merchant_id:
        # No distinction between absent and forbidden (MerchantOps §54).
        raise HTTPException(404, "Unknown incident.")
    return inc


@app.get("/incidents", response_model=schemas.IncidentList,
          response_model_exclude_unset=True)
def list_incidents(include_closed: bool = False,
                   principal: Principal = Depends(current_principal)):
    """The operations console. Scoped to the caller's merchant, ordered by
    revenue at risk — the largest problem is the one to open first."""
    with session_scope() as s:
        if include_closed:
            rows = (s.query(Incident)
                    .filter(Incident.merchant_id == principal.merchant_id)
                    .order_by(Incident.revenue_at_risk_minor.desc(),
                              Incident.detected_at.desc()).all())
        else:
            rows = open_incidents(s, principal.merchant_id)
        return {"incidents": [_incident_view(s, i) for i in rows],
                "total_revenue_at_risk_minor": sum(i.revenue_at_risk_minor for i in rows)}


@app.get("/incidents/{incident_id}", response_model=schemas.IncidentSummary,
          response_model_exclude_unset=True)
def get_incident(incident_id: str, principal: Principal = Depends(current_principal)):
    with session_scope() as s:
        return _incident_view(s, _owned_incident(s, incident_id, principal), detail=True)


@app.get("/incidents/{incident_id}/trace", response_model=schemas.IncidentTrace,
          response_model_exclude_unset=True)
def get_incident_trace(incident_id: str,
                       principal: Principal = Depends(current_principal)):
    """MerchantOps §58 — detection, every lifecycle move, and every event of
    every task the incident dispatched, in one ordering."""
    with session_scope() as s:
        _owned_incident(s, incident_id, principal)
        return {"incident_id": incident_id, "trace": trace_for_incident(s, incident_id)}


@app.get("/incidents/{incident_id}/evidence-graph",
         response_model=schemas.EvidenceGraph,
         response_model_exclude_unset=True)
def get_evidence_graph(incident_id: str,
                       principal: Principal = Depends(current_principal)):
    """MerchantOps v2 §32 — "why do you believe this?", as structure.

    The trace next door says what the system *did*. This says what it took any
    of it to *mean*, which a flat evidence list cannot: that one shows what was
    looked at, and leaves the reasoning in prose the platform did not write and
    cannot check.

    `lines` is the same graph, one line per edge, so a reader can confirm that
    nothing was added between the edges and any sentence written from them.
    """
    from app.evidence.graph import explain, why

    with session_scope() as s:
        _owned_incident(s, incident_id, principal)
        edges = explain(s, incident_id)
        return {
            "incident_id": incident_id,
            "edges": edges,
            "edge_count": sum(len(v) for v in edges.values()),
            "lines": why(s, incident_id),
        }


@app.get("/incidents/{incident_id}/hypotheses",
         response_model=schemas.HypothesisSet,
         response_model_exclude_unset=True)
def get_hypotheses(incident_id: str,
                   principal: Principal = Depends(current_principal)):
    """The competing explanations and how each fared — MerchantOps v2 §30.

    Read-only. Hypotheses are proposed and adjudicated during investigation,
    because a verdict computed at read time would differ between two people
    opening the same incident — which is the opposite of what an audit surface
    is for.

    `untested` is named rather than left to be counted off the list. A
    hypothesis nothing here can test is a gap in instrumentation, and it should
    be as visible as the verdicts beside it.
    """
    from app.evidence.hypotheses import for_incident, leading

    with session_scope() as s:
        _owned_incident(s, incident_id, principal)
        rows = for_incident(s, incident_id)
        top = leading(s, incident_id)
        return {
            "incident_id": incident_id,
            "hypotheses": [{
                "id": h.id, "label": h.label, "key": h.key,
                "statement": h.statement, "status": h.status.value,
                "proposed_by": h.proposed_by,
                "support_count": h.support_count,
                "contradiction_count": h.contradiction_count,
                "verdict_reason": h.verdict_reason,
                "adjudicated_at": (h.adjudicated_at.isoformat()
                                   if h.adjudicated_at else None),
            } for h in rows],
            "leading": top.key if top else None,
            "untested": [h.key for h in rows if h.status.value == "UNTESTED"],
        }


@app.post("/incidents/detect", response_model=schemas.DetectResult,
          response_model_exclude_unset=True)
def run_detection(principal: Principal = Depends(current_principal)):
    """Run the detection sweep for the caller's merchant.

    Idempotent: re-running over the same window records `already_known` rather
    than creating a second incident for one anomaly.
    """
    with session_scope() as s:
        return detect(s, principal.merchant_id).as_dict()


@app.post("/incidents/{incident_id}/investigate", response_model=schemas.InvestigateResult,
          response_model_exclude_unset=True)
def investigate_incident(incident_id: str,
                         principal: Principal = Depends(current_principal)):
    """Dispatch the bounded agent against an incident.

    The incident's next state is derived from the task's recorded status, never
    from the model's prose (MerchantOps §38).
    """
    with session_scope() as s:
        inc = _owned_incident(s, incident_id, principal)
        try:
            r = investigate(s, inc, principal)
        except ValueError as e:
            raise HTTPException(409, {"error": str(e),
                                      "code": "INCIDENT_NOT_OPEN"}) from e
        except PermissionError as e:
            # `from e` on a 404 too: the 404 is deliberately indistinguishable
            # to the caller (existence is not leaked), which makes the server
            # side the only place the real reason can be read.
            raise HTTPException(404, "Unknown incident.") from e
        return {"incident": _incident_view(s, r["incident"], detail=True),
                "task": _task_view(s, r["task"])}


@app.get("/recovery/ledger", response_model=schemas.LedgerView,
          response_model_exclude_unset=True)
def recovery_ledger(principal: Principal = Depends(current_principal)):
    """MerchantOps §49. Six figures that nest, in one unit.

    `invariants_broken` is returned rather than enforced by refusing to answer:
    a violated ordering is a reporting defect that has to be visible, and a
    dashboard that will not render is a dashboard nobody can use to find out why.
    """
    with session_scope() as s:
        return build_ledger(s, principal.merchant_id).as_dict()


@app.get("/dashboard", response_model=schemas.DashboardView,
          response_model_exclude_unset=True)
def merchant_dashboard(principal: Principal = Depends(current_principal)):
    """MerchantOps §50. Revenue at risk, recovery, incidents and agent activity.

    Deliberately separate from `/metrics`, which counts operations. Merging a
    revenue figure into an ops counter strip is how "12" comes to mean tasks on
    one row and rupees on the next.
    """
    with session_scope() as s:
        return dashboard(s, principal.merchant_id)


@app.get("/trace/{correlation_id}", response_model=schemas.CorrelationTrace,
          response_model_exclude_unset=True)
def get_correlation_trace(correlation_id: str,
                          principal: Principal = Depends(current_principal)):
    """MerchantOps §58. Everything one operation touched, in one ordering.

    Scoped to the caller's merchant: a trace is as much a merchant's data as
    the task it describes.
    """
    with session_scope() as s:
        events = trace_by_correlation(s, correlation_id, principal.merchant_id)
        if not events:
            raise HTTPException(404, "Unknown correlation id.")
        return {"correlation_id": correlation_id, "events": events,
                "span_count": len(events)}


@app.get("/failures/taxonomy", response_model=schemas.FailureTaxonomy,
          response_model_exclude_unset=True)
def failure_taxonomy(principal: Principal = Depends(current_principal)):
    """MerchantOps §56/§57. What every failure this system raises means, who
    owns it, and whether retrying it is sensible.

    Published rather than kept internal so an operator or an integrator can see
    that `UNKNOWN_EXTERNAL_STATE` is answered by reconciling and never by
    retrying — the single most important entry in the table.
    """
    return {"failures": [cls.as_dict(code) for code, cls in sorted(TAXONOMY.items())]}


@app.get("/metrics/prometheus")
def prometheus(request: Request,
               authorization: str | None = Header(default=None)):
    """Runtime metrics for a scraper — ADR-0031.

    Deliberately not `/metrics`, which is this merchant's business counts and
    stays that way. These are process health: no tenant scoping, because a
    latency histogram does not belong to a merchant, and therefore no principal
    to scope it to.

    **Authenticated, and not by a user.** A scraper has no merchant and should
    not be given one, so `METRICS_SCRAPE_TOKEN` is a shared secret compared in
    constant time — Prometheus sends it as `authorization: Bearer <token>`.
    Falling back to an ordinary principal would mean minting a user for a robot
    and giving it a merchant it has no business having.

    Unset, the route returns 404 rather than serving to anyone. An
    unauthenticated metrics endpoint publishes route names, traffic shape and
    error rates to whoever asks — a smaller leak than data and still a leak,
    and this project already has one route open that should not be.
    """
    expected = os.environ.get("METRICS_SCRAPE_TOKEN")
    if not expected:
        # 404 rather than 403: an unconfigured endpoint should be
        # indistinguishable from one that was never built.
        raise HTTPException(404, "Not found.")

    presented = (authorization or "")[7:].strip() \
        if (authorization or "").lower().startswith("bearer ") else ""
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(401, "Invalid scrape token.",
                            headers={"WWW-Authenticate": "Bearer"})

    body = runtime.render()
    if not runtime.counters_are_meaningful():
        body = ("# NOTE: this instance is serverless. Counters reset on every cold\n"
                "# start and no single instance sees the whole picture. Use the\n"
                "# structured logs on stdout as the operational channel here.\n") + body
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/metrics/operational", response_model=schemas.OperationalMetrics,
          response_model_exclude_unset=True)
def operational(principal: Principal = Depends(current_principal)):
    """MerchantOps §59. Measured metrics, and the ones that are not.

    The split is the point: a figure computed from nothing is worse than a
    blank, because the blank prompts a question and the number closes it.
    """
    with session_scope() as s:
        return operational_metrics(s, principal.merchant_id)


@app.get("/metrics/objectives", response_model=schemas.Objectives,
          response_model_exclude_unset=True)
def slos(principal: Principal = Depends(current_principal)):
    """MerchantOps §60. Each objective with what was measured against it —
    an SLO nobody is timing is a wish."""
    with session_scope() as s:
        return {"objectives": objectives(s, principal.merchant_id)}


@app.get("/metrics", response_model=schemas.MetricsStrip,
          response_model_exclude_unset=True)
def metrics(window_hours: int = 24,
            principal: Principal = Depends(current_principal)):
    """Counts for the operations strip, scoped to the caller's merchant.

    Everything here is derived from rows this merchant owns. There is no
    cross-merchant aggregate and there will not be one on this route: the
    isolation that applies to a task applies to a count of tasks.

    `moved_minor` deliberately counts only actions that were both executed and
    independently verified as SUCCESS. An action that is UNKNOWN has not been
    shown to have moved money, and this number must never imply it did — the
    unsettled queue is what reports those, and it reports them as unsettled.
    """
    since = utcnow() - timedelta(hours=window_hours)
    m = principal.merchant_id

    with session_scope() as s:
        gated = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "PENDING")) or 0

        approved = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "APPROVED",
                   Approval.decided_at >= since)) or 0

        rejected = s.scalar(
            select(func.count()).select_from(Approval)
            .where(Approval.merchant_id == m, Approval.decision == "REJECTED",
                   Approval.decided_at >= since)) or 0

        moved_minor = s.scalar(
            select(func.coalesce(func.sum(AgentAction.amount_minor), 0))
            .where(AgentAction.merchant_id == m,
                   AgentAction.verification_state == VerificationState.SUCCESS,
                   AgentAction.created_at >= since)) or 0

        calls, failures = s.execute(
            select(func.count(ToolCall.id),
                   func.sum(case((ToolCall.success.is_(False), 1), else_=0)))
            .join(AgentTask, AgentTask.id == ToolCall.task_id)
            .where(AgentTask.merchant_id == m, AgentTask.created_at >= since)
        ).one()
        calls = calls or 0
        failures = failures or 0

        # p50 in the database would be a percentile function three engines
        # spell differently. The row count here is small and bounded by the
        # window, so it is honest and portable to sort in Python.
        durations = sorted(
            d for (d,) in s.execute(
                select(AgentTask.duration_ms)
                .where(AgentTask.merchant_id == m, AgentTask.created_at >= since,
                       AgentTask.duration_ms.is_not(None))).all())

    p50 = durations[(len(durations) - 1) // 2] if durations else None

    return {
        "window_hours": window_hours,
        "gated": gated,
        "approved": approved,
        "rejected": rejected,
        "moved_minor": int(moved_minor),
        "tool_calls": calls,
        "tool_errors": int(failures),
        # None rather than 0.0 when nothing ran: a rate over zero calls is not
        # zero, it is unknown, and a strip cell that reads 0.0% would be a lie.
        "tool_error_rate": (failures / calls) if calls else None,
        "p50_duration_ms": p50,
        "signing_secret_is_development_default": DEV_SECRET_IN_USE,
    }


@app.get("/scenarios", response_model=list[schemas.ScenarioView],
          response_model_exclude_unset=True)
def list_scenarios():
    """The suite, including what each scenario actually asserts.

    The description is prose; `expect` is the contract. A reader deciding
    whether "106/106" means anything needs the second, and returning only the
    first left them opening the YAML to find out. Setup that changes what a
    scenario means — who runs it, an injected fault, a back-dated approval — is
    returned for the same reason.

    All of it is static configuration that ships in the repository. Nothing here
    is per-merchant or secret, which is why the route stays unauthenticated
    alongside the runner.
    """
    return [{
        "id": s.id, "category": s.category, "critical": s.critical,
        "description": s.description,
        "request": s.request,
        "principal": s.principal,
        "expect": s.expect.model_dump(exclude_defaults=True),
        "setup": {k: v for k, v in {
            "initial_state": s.initial_state or None,
            "fault": s.fault,
            "approve": s.approve,
            "approve_as": s.approve_as,
            "expire_approval": s.expire_approval or None,
            "reverify": s.reverify or None,
            "reconcile": s.reconcile or None,
            "repeat_request": s.repeat_request or None,
            "allowed_tools": s.allowed_tools,
            "budget": s.budget,
        }.items() if v is not None},
    } for s in load_scenarios()]


@app.post("/scenarios/{scenario_id}/run", response_model=schemas.ScenarioRunResult,
          response_model_exclude_unset=True)
def run_one(scenario_id: str):
    scen = {s.id: s for s in load_scenarios()}
    if scenario_id not in scen:
        raise HTTPException(404, "Unknown scenario.")
    import uuid
    settings = get_settings()
    with session_scope() as s:
        res = run_scenario(s, scen[scenario_id], f"RUN_{uuid.uuid4().hex[:8].upper()}")
        return {
            "scenario_id": res.scenario_id, "passed": res.passed,
            "checks": res.checks, "metrics": res.metrics,
            # The task the scenario produced. Without it a failing scenario is
            # a verdict with no way to see how it was reached — the trace, the
            # policy decisions and the evidence all hang off this id.
            "task_id": res.task_id,
            # Which provider produced it. An owner can switch providers at
            # runtime, and a run under a language model is not comparable to
            # the published suite even before its live-state caveat.
            "provider": settings.resolved_llm_provider,
            "model": settings.llm_model if settings.resolved_llm_provider == "anthropic"
                     else "deterministic-planner-v1",
        }
