"""The worker loop's properties, none of which are about what the jobs do.

Each job is tested where it lives. What is tested here is the thing that made
the worker worth writing rather than four cron lines: that one job failing does
not take the others down, that a job which fails instantly does not become a hot
loop, and that a stop signal is honoured between jobs rather than in the middle
of one.
"""
from __future__ import annotations

import signal

import pytest

from app import worker


def _job(name="probe", interval=10, run=None) -> worker.Job:
    return worker.Job(name, interval, run or (lambda: {"did": 1}))


# --------------------------------------------------------------------------
def test_a_job_is_due_immediately_at_startup():
    """`next_at` starts at zero so every job runs once on boot. A worker that
    starts and then does nothing for five minutes looks broken, and the person
    checking whether it works is looking in the first five minutes."""
    assert _job(interval=300).due(now=0.0)


def test_a_failing_job_does_not_raise_out_of_run_job():
    """The loop must survive it. Anything else is one bad sweep taking down the
    cadence for everything else."""
    def explode():
        raise RuntimeError("provider unreachable")

    job = _job(run=explode)
    worker.run_job(job)                      # must not raise
    assert job.failures == 1
    assert job.runs == 0


def test_consecutive_failures_are_counted_and_reset():
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise RuntimeError("down")
        return {"ok": 1}

    job = _job(run=flaky)
    worker.run_job(job)
    worker.run_job(job)
    assert job.failures == 2

    state["fail"] = False
    worker.run_job(job)
    assert job.failures == 0, "a success must clear the streak, not add to it"
    assert job.runs == 1


def test_a_failing_job_still_advances_its_schedule(monkeypatch):
    """The hot-loop guard. A job that fails in a millisecond and is not
    rescheduled is due again immediately, and starves every job behind it."""
    def explode():
        raise RuntimeError("down")

    jobs = [_job("boom", 60, explode)]
    monkeypatch.setattr(worker, "build_jobs", lambda: jobs)

    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        worker.main([])

    assert jobs[0].failures == 1, (
        f"the job ran {jobs[0].failures} times across three ticks of a "
        f"60-second interval; a failure is not rescheduling it")


def test_one_job_failing_does_not_stop_the_others(monkeypatch):
    ran = []

    def explode():
        ran.append("boom")
        raise RuntimeError("down")

    jobs = [_job("boom", 60, explode),
            _job("after", 60, lambda: ran.append("after") or {"ok": 1})]
    monkeypatch.setattr(worker, "build_jobs", lambda: jobs)
    monkeypatch.setattr(worker, "check_configuration", lambda: None, raising=False)

    worker.run_once()
    assert ran == ["boom", "after"]


def test_run_once_runs_every_job_exactly_once(monkeypatch):
    counts = {"a": 0, "b": 0}
    jobs = [_job("a", 1, lambda: counts.__setitem__("a", counts["a"] + 1) or {}),
            _job("b", 1, lambda: counts.__setitem__("b", counts["b"] + 1) or {})]
    monkeypatch.setattr(worker, "build_jobs", lambda: jobs)

    worker.run_once()
    assert counts == {"a": 1, "b": 1}


def test_a_stop_signal_is_honoured_between_jobs(monkeypatch):
    """SIGTERM sets a flag; the loop checks it. Stopping mid-drain would leave
    events claimed by a transaction that never commits."""
    stop = worker._Stop()
    handlers = {}
    monkeypatch.setattr(worker.signal, "signal",
                        lambda sig, fn: handlers.__setitem__(sig, fn))
    stop.install()

    assert not stop.requested
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.requested and stop.signal == "SIGTERM"


def test_the_loop_exits_on_a_stop_signal(monkeypatch):
    jobs = [_job("a", 3600, lambda: {"ok": 1})]
    monkeypatch.setattr(worker, "build_jobs", lambda: jobs)

    installed = {}
    monkeypatch.setattr(worker.signal, "signal",
                        lambda sig, fn: installed.__setitem__(sig, fn))
    monkeypatch.setattr(worker.time, "sleep",
                        lambda _s: installed[signal.SIGTERM](signal.SIGTERM, None))

    assert worker.main([]) == 0
    assert jobs[0].runs == 1


def test_every_scheduled_job_has_a_configured_interval():
    """The four things that were built to run repeatedly and had nothing to run
    them. A job added here without a setting would be uncadenced."""
    from app.agent.queue import WORKER_LIVENESS_SECONDS
    from app.config import get_settings

    s = get_settings()
    names = {j.name for j in worker.build_jobs()}
    assert names == {"heartbeat", "tasks", "drain", "notify", "reconcile",
                     "detect", "prune_tokens"}
    for j in worker.build_jobs():
        assert j.interval_seconds > 0, j.name
    assert s.worker_notify_interval_seconds < s.notify_approval_warning_seconds, (
        "the notification sweep must run several times inside the warning "
        "window, or a chase is delivered after the window it warned about")
    assert s.worker_heartbeat_interval_seconds * 3 <= WORKER_LIVENESS_SECONDS, (
        "a live worker must survive two missed beats, or POST /tasks starts "
        "refusing submissions while a worker is running perfectly well")
