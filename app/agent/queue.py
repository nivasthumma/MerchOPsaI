"""Accepting a task now and running it later.

A task used to execute inside the request that created it. That is the right
shape on a serverless host, where there is nothing else to run it, and the wrong
one everywhere else: a long investigation holds an HTTP connection and a worker
process for its whole duration, and the budget has to be small enough that a
proxy does not give up first. `max_wall_clock_seconds` was capped by the
platform's own timeout for exactly that reason.

So `POST /tasks` can accept a task, return `202` with an id, and let a worker
run it. The inline path stays -- it is the only one available on Vercel, and it
is what the evaluation suite uses (via `AgentRuntime` directly, not through
HTTP, so none of the 187 scenarios are affected by any of this).

## Claiming

One statement, `FOR UPDATE SKIP LOCKED`, the same mechanism the event outbox
uses. Two workers claim different tasks rather than the same one; a third
running concurrently costs a query.

## An abandoned run is failed, not retried

A worker killed mid-run leaves a task RUNNING with a lease that stops being
renewed. The obvious thing to do is put it back on the queue. This does not do
that, and the reason is money: a task that got far enough to execute an action
has already contacted a payment provider, and re-running it would re-enter a
loop whose earlier steps are invisible to the second attempt. Idempotency keys
protect the *same* action from executing twice; they do not make replaying half
an investigation safe.

So an abandoned task is marked FAILED with `WORKER_LOST` and left for a person,
which is the same posture the rest of this system takes toward an outcome it
cannot establish: UNKNOWN is resolvable, and resolving it is a human's job.
`attempts` is still recorded, because "this was claimed twice" is worth seeing.

## The lease

Long enough that a task using its whole budget is never mistaken for an
abandoned one, which means it has to be derived from the budget rather than
picked. `lease_seconds` is the enforced wall clock plus a wide margin for
everything that happens after the last turn -- output validation, the closing
audit event, the commit.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.config import get_settings
from app.models import AgentTask, TaskStatus, WorkerHeartbeat
from app.observability.logs import get_logger

log = get_logger("merchantops.queue")

#: A worker seen more recently than this is considered live. Three times the
#: heartbeat interval, so one missed pass is not an outage.
WORKER_LIVENESS_SECONDS = 90


def lease_seconds() -> int:
    """How long a claim is honoured before the task is presumed abandoned."""
    s = get_settings()
    return max(300, s.effective_wall_clock_seconds * 3)


# --------------------------------------------------------------------------
# Submitting
# --------------------------------------------------------------------------
def enqueue(session, principal, request: str, *, incident_id: str | None = None,
            scenario_id: str | None = None) -> AgentTask:
    """Accept a task for later execution.

    Writes only what is true at this moment. The §41 provenance that describes
    *what ran* -- the model, the provider, the prompt and registry versions --
    is written when the run starts, because the provider can change between now
    and then and a predicted value recorded as a measurement is the failure §41
    exists to prevent.
    """
    task = AgentTask(
        id=f"TASK_{uuid.uuid4().hex[:10].upper()}",
        merchant_id=principal.merchant_id,
        user_id=principal.user_id,
        request=request,
        status=TaskStatus.QUEUED,
        agent_version=get_settings().agent_version,
        incident_id=incident_id,
        scenario_id=scenario_id,
        queued_at=datetime.now(UTC),
    )
    session.add(task)
    session.flush()
    log.info("task_queued", extra={"task_id": task.id,
                                   "merchant_id": principal.merchant_id})
    return task


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def claim(session, *, worker_id: str) -> AgentTask | None:
    """Take the oldest queued task, or None.

    A single UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1),
    so the read and the write cannot be separated by another worker's claim.
    Ordered by `queued_at` rather than `created_at`: they agree today, and they
    would stop agreeing the moment anything re-queues a task.
    """
    row = session.execute(text("""
        UPDATE agent_tasks SET
            status      = 'RUNNING',
            claimed_by  = :w,
            claimed_at  = now(),
            started_at  = now(),
            attempts    = attempts + 1
        WHERE id = (
            SELECT id FROM agent_tasks
            WHERE status = 'QUEUED'
            ORDER BY queued_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
    """), {"w": worker_id}).first()
    if row is None:
        return None
    session.flush()
    return session.get(AgentTask, row[0])


def reclaim_abandoned(session, *, now: datetime | None = None) -> list[str]:
    """Fail tasks whose worker is not coming back. Returns their ids.

    Only RUNNING tasks with a stale lease. AWAITING_APPROVAL is not RUNNING, so
    a task waiting on a human is never touched however long it waits -- which is
    the point of it being a separate state.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=lease_seconds())

    rows = session.execute(text("""
        UPDATE agent_tasks SET
            status       = 'FAILED',
            failure_code = 'WORKER_LOST',
            final_answer = 'The worker running this task stopped before it '
                           'finished. The partial trace is preserved. It was '
                           'not restarted automatically: a run that got far '
                           'enough to contact the payment provider cannot be '
                           'safely replayed from the beginning.'
        WHERE status = 'RUNNING'
          AND claimed_at IS NOT NULL
          AND claimed_at < :cutoff
        RETURNING id, claimed_by
    """), {"cutoff": cutoff}).all()
    session.flush()

    for task_id, worker in rows:
        log.error("task_abandoned", extra={"task_id": task_id, "queue": {
            "claimed_by": worker, "lease_seconds": lease_seconds(),
            "action": "failed as WORKER_LOST; not retried"}})
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# Depth, and whether anything is running
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class QueueState:
    queued: int
    running: int
    #: Age of the oldest queued task. The number that says a queue is stuck --
    #: depth alone cannot, because a deep queue that is moving is healthy and a
    #: single task that has waited an hour is not.
    oldest_queued_seconds: int | None
    worker_seen_seconds_ago: int | None

    @property
    def worker_is_live(self) -> bool:
        return (self.worker_seen_seconds_ago is not None
                and self.worker_seen_seconds_ago <= WORKER_LIVENESS_SECONDS)

    def as_dict(self) -> dict:
        return {"queued": self.queued, "running": self.running,
                "oldest_queued_seconds": self.oldest_queued_seconds,
                "worker_seen_seconds_ago": self.worker_seen_seconds_ago,
                "worker_is_live": self.worker_is_live}


def queue_state(session) -> QueueState:
    counts = dict(session.execute(text("""
        SELECT status, count(*) FROM agent_tasks
        WHERE status IN ('QUEUED', 'RUNNING') GROUP BY status
    """)).all())

    oldest = session.execute(
        select(func.min(AgentTask.queued_at)).where(
            AgentTask.status == TaskStatus.QUEUED)).scalar()
    seen = session.execute(select(func.max(WorkerHeartbeat.last_seen_at))).scalar()

    now = datetime.now(UTC)

    def _age(when):
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, int((now - when).total_seconds()))

    return QueueState(
        queued=int(counts.get("QUEUED", 0)),
        running=int(counts.get("RUNNING", 0)),
        oldest_queued_seconds=_age(oldest),
        worker_seen_seconds_ago=_age(seen),
    )


def heartbeat(session, *, worker_id: str, jobs: list[str]) -> None:
    """Say this worker is alive. Upsert, so a restart reuses its row."""
    session.execute(text("""
        INSERT INTO worker_heartbeats (id, last_seen_at, jobs, started_at)
        VALUES (:id, now(), CAST(:jobs AS json), now())
        ON CONFLICT (id) DO UPDATE
            SET last_seen_at = now(), jobs = EXCLUDED.jobs
    """), {"id": worker_id, "jobs": __import__("json").dumps(jobs)})
    session.flush()
