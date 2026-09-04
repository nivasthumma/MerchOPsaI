"""The cadence. Everything in this system that runs on one, runs here.

    python -m app.worker            # loop until stopped
    python -m app.worker --once     # every job once, then exit

Four things were built to run repeatedly and had nothing to run them:

  drain       events sit PENDING in `event_outbox` until somebody delivers
              them, and the notification consumers hang off that delivery
  notify      an approval expiring is the absence of a decision, so no event
              fires -- it has to be looked for
  reconcile   an action left UNKNOWN settles when its provider state is
              re-read, which nothing was doing
  detect      incidents are found by a sweep over payment history

Each was reachable by hand (`make reconcile`, `POST /events/drain`) and by
nothing else. The README called this out for detection and reconciliation and
was right to; this closes it for all four.

## Why a loop and not cron

Cron is fine and this is not a replacement for it -- `--once` exists precisely
so a platform scheduler can drive the same code. The loop earns its place by
being one process to run in a container next to the API, with no second system
to configure, and by making the intervals a property of the application rather
than of whatever host it landed on.

## What it guarantees

**One job at a time.** Sequential, single-threaded, no overlap. The drain claims
rows `FOR UPDATE SKIP LOCKED` and the notification sends deduplicate on a UNIQUE
constraint, so a second worker is safe -- but within one worker there is no
value in concurrency and considerable value in a log you can read top to bottom.

**A failing job never stops the others, and never spins.** An exception is
logged and the schedule advances anyway. Without advancing, a job that fails
instantly becomes a hot loop that starves everything behind it.

**SIGTERM finishes the current job.** A container stop mid-drain would otherwise
leave events claimed by a transaction that never commits. The signal sets a
flag; the loop checks it between jobs.

## Scope

Detection is called once per merchant, in a loop, never as a cross-merchant
query -- `app.detection.engine.detect` takes a merchant by argument "never by
discovery" (MerchantOps §54), and enumerating merchants here keeps that true.
The enumeration is the worker's business; the read stays scoped.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import text

from app import authz, tenancy
from app.audit.trace import correlation_scope
from app.config import get_settings
from app.db import session_scope
from app.models import TaskStatus
from app.observability.logs import configure_logging, get_logger

log = get_logger("merchantops.worker")

#: Identifies this worker in `worker_heartbeats` and on the tasks it claims.
#: Host plus pid, so two workers in one container are distinguishable and a
#: restart is visibly a restart rather than a second worker.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def job_drain() -> dict:
    from app.events.bus import drain

    with session_scope() as s:
        return drain(s, limit=500)


def job_notify() -> dict:
    from app.notify import sweep
    from app.notify.service import retry_pending

    with session_scope() as s:
        report = sweep(s)
    with session_scope() as s:
        report["retried"] = retry_pending(s).as_dict()
    return report


def job_reconcile() -> dict:
    from app.verification.reconciler import reconcile

    with session_scope() as s:
        return reconcile(s, min_age_seconds=30, max_attempts=5, limit=100).as_dict()


def job_detect() -> dict:
    from app.detection.engine import detect

    with session_scope() as s:
        merchants = [r[0] for r in s.execute(
            text("SELECT id FROM merchants ORDER BY id")).all()]

    opened = already = 0
    for merchant_id in merchants:
        # A session per merchant, so one merchant's detection failing does not
        # roll back another's incidents.
        with session_scope() as s:
            report = detect(s, merchant_id)
            opened += report.incidents_created
            already += report.already_known
    # `already_known` reported alongside, because detection is idempotent by
    # design and a sweep that opens nothing because everything is already open
    # is a different state from one that found nothing at all.
    return {"merchants": len(merchants), "incidents_opened": opened,
            "already_known": already}


def job_tasks() -> dict:
    """Run whatever has been queued, then fail whatever was abandoned.

    Claims one at a time and runs it to completion before claiming the next.
    A task is the expensive unit in this system -- a model loop, a dozen
    database round trips, possibly an outbound payment call -- so the useful
    concurrency is more workers, not more threads inside one. Two workers claim
    different rows (`FOR UPDATE SKIP LOCKED`) with nothing else required.

    Bounded per pass so one busy queue cannot starve the sweeps behind it. What
    is left stays queued and is picked up on the next pass, a few seconds later.
    """
    from app.agent.queue import claim, reclaim_abandoned
    from app.agent.runtime import AgentRuntime, AgentRuntimeError, Principal

    ran = failed = 0
    for _ in range(get_settings().worker_max_tasks_per_pass):
        with session_scope() as s:
            task = claim(s, worker_id=WORKER_ID)
            if task is None:
                break

            # The principal is rebuilt from the row's user, never carried from
            # the request that submitted it. Authority is read at the moment
            # authority is used, exactly as `current_principal` does per
            # request -- a task queued an hour ago must not run with permissions
            # its submitter has since lost.
            row = authz.resolve(s, task.user_id)
            if row is None:
                task.status = TaskStatus.FAILED
                task.failure_code = "PRINCIPAL_GONE"
                task.final_answer = (
                    "The user who submitted this task no longer exists, so it "
                    "cannot be run with their authority.")
                failed += 1
                continue

            principal = Principal(row.tenant_id, row.user_id, row.merchant_id,
                                  row.role, list(row.permissions))
            try:
                # Bound for the run, the same way an HTTP request is, so the
                # database filters this task's queries to its own merchant
                # (ADR-0046). A worker is the one place where one process runs
                # work for several merchants in sequence, which makes it the
                # place where a leaked binding would matter most -- the context
                # manager sheds it, so the sweeps that follow still see
                # everything.
                with tenancy.scoped(principal.tenant_id, principal.merchant_id):
                    AgentRuntime(s, principal).run(task.request, existing_task=task)
                ran += 1
            except AgentRuntimeError:
                # Already recorded on the task, with its partial trace committed
                # -- that is what AgentRuntime._crash exists for. Counted here
                # and not re-raised: one task failing must not stop the queue.
                failed += 1

    abandoned = []
    with session_scope() as s:
        abandoned = reclaim_abandoned(s)

    return {"ran": ran, "failed": failed, "abandoned": len(abandoned)}


def job_prune_tokens() -> dict:
    """Drop revocations for tokens that have expired anyway.

    A denylist that only grows eventually costs more than what it protects.
    Once a token's own `exp` has passed the signature check refuses it without
    consulting the table at all, so the row buys nothing after that.
    """
    from app.auth import prune_revoked
    from app.sso import prune_flows

    with session_scope() as s:
        # Sign-in attempts nobody completed, alongside the token denylist.
        # Both are rows that stop mattering at a known moment.
        return {"pruned": prune_revoked(s), "abandoned_sign_ins": prune_flows(s)}


def job_heartbeat() -> dict:
    """Say this worker is alive, so its absence is visible.

    Everything cadence-driven runs here. If this process is not running, nothing
    sweeps and no queued task starts -- and the absence of work looks exactly
    like there being no work to do. `/health` reads this, and `POST /tasks`
    refuses an asynchronous submission when no worker has been seen recently,
    rather than queueing into a void.
    """
    from app.agent.queue import heartbeat

    with session_scope() as s:
        heartbeat(s, worker_id=WORKER_ID, jobs=[j.name for j in build_jobs()])
    return {}


@dataclass
class Job:
    name: str
    interval_seconds: int
    run: Callable[[], dict]
    #: Monotonic time of the next run. Zero means "due now", so every job runs
    #: once at startup rather than after one full interval -- a worker that
    #: starts and then does nothing for five minutes looks broken.
    next_at: float = 0.0
    failures: int = 0
    runs: int = 0

    def due(self, now: float) -> bool:
        return now >= self.next_at


def build_jobs() -> list[Job]:
    s = get_settings()
    return [
        # First in the list, so a pass that is about to do work says so before
        # doing it rather than after.
        Job("heartbeat", s.worker_heartbeat_interval_seconds, job_heartbeat),
        Job("tasks", s.worker_tasks_interval_seconds, job_tasks),
        Job("drain", s.worker_drain_interval_seconds, job_drain),
        Job("notify", s.worker_notify_interval_seconds, job_notify),
        Job("reconcile", s.worker_reconcile_interval_seconds, job_reconcile),
        Job("detect", s.worker_detect_interval_seconds, job_detect),
        # Hourly. The table only grows by one row per explicit sign-out, so
        # this is housekeeping rather than a load-bearing sweep.
        Job("prune_tokens", 3600, job_prune_tokens),
    ]


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------
@dataclass
class _Stop:
    """Set by SIGTERM/SIGINT, read between jobs."""
    requested: bool = False
    signal: str = field(default="")

    def install(self) -> None:
        def handler(signum, _frame):
            self.requested = True
            self.signal = signal.Signals(signum).name
            log.info("worker_stopping", extra={"worker": {"signal": self.signal}})

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, handler)


def run_job(job: Job) -> None:
    """One job, timed and logged. Never raises."""
    started = time.monotonic()
    # Its own correlation id, so everything one sweep wrote can be found
    # together -- the same property a request gets from the middleware.
    with correlation_scope(f"COR_{uuid.uuid4().hex[:12].upper()}"):
        try:
            result = job.run()
        except Exception as exc:
            job.failures += 1
            log.error("worker_job_failed", extra={"worker": {
                "job": job.name, "consecutive_failures": job.failures,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }}, exc_info=True)
        else:
            job.runs += 1
            job.failures = 0
            duration = round((time.monotonic() - started) * 1000, 2)
            # Only when it did something. A drain that found nothing, every five
            # seconds, is a log nobody can read and therefore a log in which
            # nothing can be found.
            if any(v for v in result.values() if isinstance(v, int) and v) or \
                    any(isinstance(v, dict) and any(v.values()) for v in result.values()):
                log.info("worker_job", extra={"worker": {
                    "job": job.name, "duration_ms": duration, "result": result}})


def run_once() -> None:
    """Every job once, in order. What `--once` and the tests call."""
    for job in build_jobs():
        run_job(job)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true",
                    help="Run every job once and exit. For a platform scheduler.")
    ap.add_argument("--tick-seconds", type=float, default=1.0,
                    help="How often to check whether a job is due (default 1).")
    args = ap.parse_args(argv)

    configure_logging()

    # Same validation the API does at import. A worker is the process that sends
    # most of the notifications, and discovering NOTIFY_CHANNELS is wrong on the
    # first approval that needed a human is discovering it too late.
    from app.notify import check_configuration, register

    check_configuration()
    register()

    if args.once:
        run_once()
        return 0

    jobs = build_jobs()
    stop = _Stop()
    stop.install()

    log.info("worker_started", extra={"worker": {
        "jobs": {j.name: j.interval_seconds for j in jobs}}})

    while not stop.requested:
        now = time.monotonic()
        for job in jobs:
            if stop.requested:
                break
            if not job.due(now):
                continue
            run_job(job)
            # Advanced after the run and regardless of outcome: from the end, so
            # a slow job does not immediately become due again; regardless, so a
            # job failing instantly does not become a hot loop.
            job.next_at = time.monotonic() + job.interval_seconds
        time.sleep(args.tick_seconds)

    log.info("worker_stopped", extra={"worker": {
        "signal": stop.signal,
        "runs": {j.name: j.runs for j in jobs}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
