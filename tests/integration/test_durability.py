"""What survives a failure — ADR-0029.

Every test here asserts the same thing from a different angle: the record of
what happened outlives the request that produced it. A system whose evidence
disappears exactly when something goes wrong has evidence of nothing worth
having.
"""
from __future__ import annotations

import threading

from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime, AgentRuntimeError
from app.audit.trace import current_correlation_id, set_correlation_id
from app.llm.deterministic import DeterministicProvider
from app.models import ActionStatus, AgentAction, AgentTask, AuditLog, Refund, TaskStatus
from app.verification.reconciler import (
    abandoned_claim_age_seconds,
    escalated_actions,
    find_unsettled,
    reconcile,
)


def _age(session, action_id: str, *, seconds: int) -> None:
    """Backdate an action so a sweep will consider it. Reaching for the column
    directly rather than sleeping: the guard is a duration, and a test that
    waits out seventy seconds to prove it is not a test anyone runs."""
    session.execute(
        text("UPDATE agent_actions SET updated_at = now() - (:s * interval '1 second') "
             "WHERE id = :a"),
        {"s": seconds, "a": action_id})
    session.flush()
    session.expire_all()


class _RaisesOnTurn(DeterministicProvider):
    """A provider that plans normally, then breaks on the nth turn."""

    def __init__(self, fail_on: int = 2):
        self._fail_on = fail_on
        self._turns = 0

    def turn(self, **kw):
        self._turns += 1
        if self._turns >= self._fail_on:
            raise RuntimeError("provider exploded")
        return super().turn(**kw)


# ------------------------------------------------------- the crash boundary
def test_an_unhandled_error_still_leaves_a_task_to_look_at(db, owner):
    """The failure this whole change exists for.

    Before the boundary, a provider raising here propagated to `session_scope`,
    which rolled the request back and destroyed the task, its tool calls and its
    audit events on the way out. The run had happened and left nothing.
    """
    try:
        AgentRuntime(db, owner, provider=_RaisesOnTurn()).run("Why did revenue drop?")
    except AgentRuntimeError as exc:
        crash = exc
    else:
        raise AssertionError("the run should have failed")

    assert crash.task_id is not None
    assert crash.persisted is True

    # Exactly what the API's session_scope does on the way out. The trace has to
    # be there on the other side of it.
    db.rollback()

    task = db.get(AgentTask, crash.task_id)
    assert task is not None, "the rollback took the task with it"
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "INTERNAL_ERROR"

    events = [a.event_type for a in db.query(AuditLog)
              .filter(AuditLog.task_id == crash.task_id).all()]
    assert "task_created" in events
    assert "task_crashed" in events


def test_the_crash_record_does_not_publish_the_error_text(db, owner):
    """§37: the detail is audited, redacted; it is not handed to the caller."""
    class _LeaksASecret(DeterministicProvider):
        def turn(self, **kw):
            raise RuntimeError("failed calling https://api.example.com key=rzp_test_ABC123")

    try:
        AgentRuntime(db, owner, provider=_LeaksASecret()).run("Why did revenue drop?")
    except AgentRuntimeError as exc:
        crash = exc
    db.rollback()

    row = (db.query(AuditLog)
           .filter(AuditLog.task_id == crash.task_id,
                   AuditLog.event_type == "task_crashed").one())
    assert "rzp_test_ABC123" not in str(row.payload)
    assert "[REDACTED]" in str(row.payload)


def test_a_failed_task_is_classified_rather_than_left_generic(db, owner):
    """§56 wants six fields on every failure, including the ones nobody planned."""
    try:
        AgentRuntime(db, owner, provider=_RaisesOnTurn()).run("Why did revenue drop?")
    except AgentRuntimeError as exc:
        crash = exc
    db.rollback()

    row = (db.query(AuditLog)
           .filter(AuditLog.task_id == crash.task_id,
                   AuditLog.event_type == "task_crashed").one())
    assert row.payload["failure"]["retryability"] == "ESCALATE"


# ------------------------------------------------- the claim before the call
def test_the_action_claim_outlives_the_request_that_made_it(db, owner):
    """The order the architecture promises: claim, commit, then call.

    A flushed reservation dies with the transaction. If that happened after the
    provider accepted the refund, the money would have moved and nothing would
    say who moved it — which is the one state `agent_actions` exists to prevent.
    """
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    assert out.status is TaskStatus.AWAITING_APPROVAL

    r = approve_and_execute(db, out.task.id, owner)
    action_id = r["action"].id

    # Everything after the reservation is discarded, exactly as an exception on
    # the way back would discard it.
    db.rollback()

    row = db.execute(text("SELECT id, idempotency_key FROM agent_actions WHERE id = :a"),
                     {"a": action_id}).mappings().first()
    assert row is not None, "the claim was rolled back with the request"
    assert row["idempotency_key"]


def test_a_lost_request_leaves_a_claim_the_sweep_can_finish(db, owner):
    """What is left behind is not an orphan.

    Rolling back after the claim reproduces a request that died mid-action: the
    reservation is committed, everything written after it is gone, so the row
    sits PENDING with an idempotency key and no outcome. That is the state
    reconciliation exists for, and before this it was a row nothing looked at.
    """
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    action_id = r["action"].id
    db.rollback()

    action = db.get(AgentAction, action_id)
    assert action.status is ActionStatus.PENDING
    assert action.verification_state is None
    assert action.idempotency_key, "nothing to ask the provider about"

    # Too young: a request this age may still be running and about to write the
    # outcome we would be overwriting.
    assert action not in find_unsettled(db, min_age_seconds=0)

    # Older than any request can live, and the sweep claims it.
    _age(db, action_id, seconds=abandoned_claim_age_seconds() + 60)
    assert action_id in [a.id for a in find_unsettled(db, min_age_seconds=0)]


def test_the_sweep_settles_an_abandoned_claim_by_its_key(db, owner):
    """Settlement is a read. It asks the provider about our own key and records
    the answer; it never re-issues the action."""
    before = db.query(Refund).count()
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    action_id = r["action"].id
    db.rollback()

    _age(db, action_id, seconds=abandoned_claim_age_seconds() + 60)
    report = reconcile(db, min_age_seconds=0)

    assert report.scanned >= 1
    action = db.get(AgentAction, action_id)
    assert action.verification_state is not None, "still nobody's problem"
    # No second refund. The sweep reads; it does not act.
    assert db.query(Refund).count() == before


def test_an_unsettleable_claim_reaches_the_operator_queue(db, owner):
    """Exhausting the attempts must make it visible, not make it disappear."""
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    action_id = r["action"].id
    db.rollback()

    # A claim nothing ever managed to establish an outcome for: attempts spent,
    # verification_state still NULL.
    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :a"),
               {"a": action_id})
    db.flush()

    assert action_id in [x["id"] for x in escalated_actions(db, max_attempts=5)]


# ------------------------------------------------------------- the deadline
def test_the_budget_is_held_inside_the_hosts_own_timeout():
    """Two numbers in two files used to disagree, and the host won every time."""
    from app.config import PLATFORM_MARGIN_SECONDS, Settings

    unhosted = Settings(max_wall_clock_seconds=60, platform_timeout_seconds=None)
    assert unhosted.effective_wall_clock_seconds == 60

    hosted = Settings(max_wall_clock_seconds=60, platform_timeout_seconds=30)
    assert hosted.effective_wall_clock_seconds == 30 - PLATFORM_MARGIN_SECONDS

    # A budget already inside the limit is left alone, not stretched up to it.
    modest = Settings(max_wall_clock_seconds=15, platform_timeout_seconds=300)
    assert modest.effective_wall_clock_seconds == 15

    # Never zero or negative, however tight the host is.
    tiny = Settings(max_wall_clock_seconds=60, platform_timeout_seconds=1)
    assert tiny.effective_wall_clock_seconds >= 5


def test_a_turn_is_given_the_time_that_is_actually_left(db, owner):
    """The wall clock bounds the run, not merely the gaps between calls."""
    seen: list[float | None] = []

    class _Records(DeterministicProvider):
        def turn(self, **kw):
            seen.append(kw.get("timeout"))
            return super().turn(**kw)

    AgentRuntime(db, owner, provider=_Records()).run("Why did revenue drop this week?")

    assert seen, "no turn was taken"
    assert all(t is not None for t in seen), "a turn ran with no deadline"
    assert all(0 < t <= 60 for t in seen)
    # Each turn gets less than the one before it: the budget is being spent.
    assert seen == sorted(seen, reverse=True)


# ------------------------------------------------------ the correlation id
def test_two_concurrent_runs_do_not_share_a_correlation_id():
    """It was a module global, and `def` endpoints run in a threadpool.

    Two tasks genuinely execute at once, so a global had them overwrite each
    other — joining two unrelated traces into one, which is the single thing a
    correlation id must never do.
    """
    read: dict[str, str | None] = {}
    started = threading.Barrier(2)

    def run(name: str) -> None:
        set_correlation_id(f"COR_{name}")
        started.wait(timeout=5)      # both have written before either reads
        read[name] = current_correlation_id()

    threads = [threading.Thread(target=run, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert read == {"A": "COR_A", "B": "COR_B"}


def test_a_finished_run_leaves_no_correlation_id_behind(db, owner):
    """Including one that failed — the next run on this thread is not this run."""
    set_correlation_id(None)
    try:
        AgentRuntime(db, owner, provider=_RaisesOnTurn()).run("Why did revenue drop?")
    except AgentRuntimeError:
        pass
    assert current_correlation_id() is None


# ---------------------------------------------------- the boundary at the API
def test_the_api_reports_a_crash_with_a_task_to_open(db, monkeypatch):
    """A 500 that names nothing is a run an operator cannot investigate."""
    from fastapi.testclient import TestClient

    import app.agent.runtime as rt
    from app.api import security as sec
    from app.api.main import app

    sec.reset_rate_limits()
    # The endpoint builds its own runtime, so the break has to go in underneath.
    monkeypatch.setattr(rt, "get_provider", lambda: _RaisesOnTurn(fail_on=1))

    # `raise_server_exceptions=False` so the app's own handler produces the
    # response, which is the thing under test.
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/tasks", json={"request": "Why did revenue drop?"},
                   headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"})

    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert detail["task_id"], "nothing to open"
    assert detail["trace_preserved"] is True
    # §56: an operator is told whether trying again is even the question.
    assert detail["failure"]["retryability"] == "ESCALATE"

    # And the error text itself is not in the response.
    assert "provider exploded" not in r.text

    db.rollback()
    assert db.get(AgentTask, detail["task_id"]) is not None
    sec.reset_rate_limits()
