"""Accepting a task now and running it later — ADR-0045.

The interesting cases are not "does it run". They are the ones where the queue
and the thing draining it disagree: two workers reaching for the same task, a
worker that dies holding one, a task whose submitter has lost the permission it
needs, and a submission made when nothing is draining the queue at all.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.agent.queue import (
    WORKER_LIVENESS_SECONDS,
    claim,
    enqueue,
    heartbeat,
    lease_seconds,
    queue_state,
    reclaim_abandoned,
)
from app.models import AgentTask, TaskStatus


def _queued(db, owner, request="why did revenue drop") -> AgentTask:
    return enqueue(db, owner, request)


@pytest.fixture
def client(db):
    """An authenticated caller, on the suite's transaction."""
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"})
        yield c
    sec.reset_rate_limits()


# --------------------------------------------------------------------------
# Accepting
# --------------------------------------------------------------------------
def test_a_queued_task_records_no_model_version(db, owner):
    """§41 records what actually RAN. A queued task has not run, and the
    provider can change before it does -- `POST /config/llm-provider` exists to
    do exactly that. A model version written here would be a prediction stored
    where a measurement belongs."""
    task = _queued(db, owner)
    assert task.status is TaskStatus.QUEUED
    assert task.model_version is None
    assert task.model_provider is None
    assert task.prompt_version is None
    assert task.queued_at is not None
    assert task.started_at is None


def test_the_provenance_is_written_when_it_runs(db, owner):
    from app.agent.runtime import AgentRuntime

    task = _queued(db, owner)
    claimed = claim(db, worker_id="w1")
    AgentRuntime(db, owner).run(claimed.request, existing_task=claimed)

    db.refresh(task)
    assert task.model_version is not None
    assert task.model_provider is not None
    assert task.prompt_version is not None
    assert task.started_at is not None


def test_the_row_is_the_authority_on_what_was_asked(db, owner):
    """A worker passing a different request string must not silently run
    something other than what was accepted."""
    from app.agent.runtime import AgentRuntime

    task = _queued(db, owner, "investigate the UPI drop")
    claimed = claim(db, worker_id="w1")
    AgentRuntime(db, owner).run("something else entirely", existing_task=claimed)

    db.refresh(task)
    assert task.request == "investigate the UPI drop"


# --------------------------------------------------------------------------
# Claiming
# --------------------------------------------------------------------------
def test_claiming_marks_who_holds_it_and_since_when(db, owner):
    _queued(db, owner)
    task = claim(db, worker_id="worker-a")
    assert task.status is TaskStatus.RUNNING
    assert task.claimed_by == "worker-a"
    assert task.claimed_at is not None
    assert task.attempts == 1


def test_a_claimed_task_is_not_claimed_again(db, owner):
    """Within one session this is the ordering guarantee; across sessions it is
    SKIP LOCKED. Either way a task runs once."""
    _queued(db, owner)
    first = claim(db, worker_id="worker-a")
    assert first is not None
    assert claim(db, worker_id="worker-b") is None


def test_the_oldest_queued_task_goes_first(db, owner):
    a = _queued(db, owner, "first")
    b = _queued(db, owner, "second")
    db.execute(text("UPDATE agent_tasks SET queued_at = :t WHERE id = :i"),
               {"t": datetime.now(UTC) - timedelta(minutes=5), "i": a.id})
    db.flush()

    assert claim(db, worker_id="w").id == a.id
    assert claim(db, worker_id="w").id == b.id


def test_an_empty_queue_claims_nothing(db):
    assert claim(db, worker_id="w") is None


# --------------------------------------------------------------------------
# Abandonment
# --------------------------------------------------------------------------
def test_a_task_whose_worker_died_is_failed_not_retried(db, owner):
    """The decision that matters. A run that got far enough to contact the
    payment provider cannot be safely replayed from the beginning: idempotency
    keys stop the same action executing twice, they do not make replaying half
    an investigation safe. So it is failed and left for a person."""
    _queued(db, owner)
    task = claim(db, worker_id="doomed")
    db.execute(text("UPDATE agent_tasks SET claimed_at = :t WHERE id = :i"),
               {"t": datetime.now(UTC) - timedelta(seconds=lease_seconds() + 60),
                "i": task.id})
    db.flush()

    assert reclaim_abandoned(db) == [task.id]
    db.refresh(task)
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "WORKER_LOST"
    assert task.attempts == 1, "it must not have been re-queued and re-claimed"


def test_a_task_inside_its_lease_is_left_alone(db, owner):
    """The lease is derived from the execution budget, so a task using all of
    its allowance is never mistaken for an abandoned one."""
    _queued(db, owner)
    task = claim(db, worker_id="busy")
    assert reclaim_abandoned(db) == []
    db.refresh(task)
    assert task.status is TaskStatus.RUNNING


def test_a_task_waiting_for_a_human_is_never_reclaimed(db, owner):
    """AWAITING_APPROVAL is not RUNNING, and a person may take a long time. If
    the lease applied to it, an approval queue would fail itself."""
    _queued(db, owner)
    task = claim(db, worker_id="w")
    task.status = TaskStatus.AWAITING_APPROVAL
    db.execute(text("UPDATE agent_tasks SET claimed_at = :t WHERE id = :i"),
               {"t": datetime.now(UTC) - timedelta(days=2), "i": task.id})
    db.flush()

    assert reclaim_abandoned(db) == []
    db.refresh(task)
    assert task.status is TaskStatus.AWAITING_APPROVAL


def test_the_lease_exceeds_the_execution_budget(db):
    from app.config import get_settings

    assert lease_seconds() > get_settings().effective_wall_clock_seconds, (
        "a task using its full budget would be reclaimed while still running")


# --------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------
def test_queue_state_reports_depth_and_the_oldest_wait(db, owner):
    _queued(db, owner)
    old = _queued(db, owner)
    db.execute(text("UPDATE agent_tasks SET queued_at = :t WHERE id = :i"),
               {"t": datetime.now(UTC) - timedelta(seconds=90), "i": old.id})
    db.flush()

    state = queue_state(db)
    assert state.queued == 2
    assert state.oldest_queued_seconds >= 89, (
        "depth alone cannot say a queue is stuck; the oldest wait can")


def test_no_heartbeat_means_no_live_worker(db):
    db.execute(text("DELETE FROM worker_heartbeats"))
    db.flush()
    state = queue_state(db)
    assert state.worker_is_live is False
    assert state.worker_seen_seconds_ago is None


def test_a_recent_heartbeat_means_a_live_worker(db):
    heartbeat(db, worker_id="w1", jobs=["tasks", "drain"])
    state = queue_state(db)
    assert state.worker_is_live is True
    assert state.worker_seen_seconds_ago <= 5


def test_a_stale_heartbeat_does_not_count_as_alive(db):
    heartbeat(db, worker_id="w1", jobs=["tasks"])
    db.execute(text("UPDATE worker_heartbeats SET last_seen_at = :t"),
               {"t": datetime.now(UTC) - timedelta(seconds=WORKER_LIVENESS_SECONDS + 30)})
    db.flush()
    assert queue_state(db).worker_is_live is False


def test_a_restarting_worker_reuses_its_row(db):
    """Upsert, not insert. Otherwise a container that restarts hourly leaves a
    table of dead workers and `max(last_seen_at)` keeps reading healthy."""
    heartbeat(db, worker_id="w1", jobs=["tasks"])
    heartbeat(db, worker_id="w1", jobs=["tasks", "drain"])
    n = db.execute(text("SELECT count(*) FROM worker_heartbeats WHERE id='w1'")).scalar()
    assert n == 1


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------
def test_async_is_refused_when_nothing_is_draining_the_queue(client):
    """202 for a task that will never start is worse than a refusal. A queue
    nobody is draining looks exactly like a queue with nothing in it, and this
    is the response that tells them apart."""
    from app.db import session_scope

    with session_scope() as s:
        s.execute(text("DELETE FROM worker_heartbeats"))

    r = client.post("/tasks?mode=async", json={"request": "why did revenue drop"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "no_worker"


def test_async_accepts_with_202_when_a_worker_is_live(client):
    from app.db import session_scope

    # Committed for real: the route opens its own session and cannot see a
    # heartbeat written inside the suite's uncommitted transaction.
    with session_scope() as s:
        heartbeat(s, worker_id="w-live", jobs=["tasks"])
    try:
        r = client.post("/tasks?mode=async", json={"request": "why did revenue drop"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "QUEUED"
        assert body["id"].startswith("TASK_")
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM worker_heartbeats"))


def test_inline_still_runs_the_task_in_the_request(client):
    """The default, and the only thing possible where there is no worker. This
    route's behaviour must not have changed for anybody who did not ask."""
    r = client.post("/tasks?mode=inline", json={"request": "why did revenue drop"})
    assert r.status_code == 200
    assert r.json()["status"] in {"COMPLETED", "AWAITING_APPROVAL", "DENIED"}


def test_an_unknown_mode_is_refused(client):
    r = client.post("/tasks?mode=sideways", json={"request": "x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_mode"


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------
def test_a_queued_task_runs_with_authority_read_at_run_time(db, owner, monkeypatch):
    """Not with the permissions its submitter held when they submitted it.

    A task queued an hour ago must not execute with authority the submitter has
    since lost. `current_principal` re-reads permissions from the database on
    every request for the same reason; the worker rebuilds the principal from
    the row's user for the same reason again.
    """
    import app.worker as worker

    _queued(db, owner)
    db.execute(text("UPDATE users SET permissions = :p WHERE id = :u"),
               {"p": '[]', "u": owner.user_id})
    db.commit()

    captured = {}

    class _Spy:
        def __init__(self, session, principal, **kw):
            captured["permissions"] = list(principal.permissions)

        def run(self, request, **kw):
            return None

    monkeypatch.setattr("app.agent.runtime.AgentRuntime", _Spy)
    monkeypatch.setattr(worker, "session_scope", lambda: _Session(db))
    worker.job_tasks()

    assert captured["permissions"] == [], (
        "the run used the permissions carried from submission, not the ones the "
        "user holds now")


class _Session:
    """The suite's transaction, handed to code that opens its own."""

    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False
