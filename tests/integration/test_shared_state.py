"""Rate limiting and the provider override, across replicas.

These tests need a real Redis and skip without one. That is deliberate: the
behaviour under test is "several processes agree", and a fake that agrees with
itself would pass while proving nothing. `REDIS_URL` is set in CI.

The one that matters is `test_two_replicas_share_one_budget`. Everything else
here is about degrading safely when Redis is not there or stops answering.
"""
from __future__ import annotations

import os

import pytest

from app import shared_state
from app.api import security

REDIS_URL = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
requires_redis = pytest.mark.skipif(
    not REDIS_URL, reason="needs a real Redis; set TEST_REDIS_URL or REDIS_URL")


@pytest.fixture
def redis_state(monkeypatch):
    """A clean shared backend, torn down whichever way the test ends."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    shared_state.reset_for_tests()
    client = shared_state.get_client()
    client.flushdb()
    yield client
    client.flushdb()
    shared_state.reset_for_tests()


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    shared_state.reset_for_tests()
    security.reset_rate_limits()
    yield
    shared_state.reset_for_tests()


# --------------------------------------------------------------------------
# The point of the exercise
# --------------------------------------------------------------------------
@requires_redis
def test_two_replicas_share_one_budget(redis_state):
    """The defect this replaces: N replicas served N times the configured limit.

    Two independent `consume` callers are two API processes as far as Redis is
    concerned -- there is no per-process state in the shared path at all.
    """
    allowed = 0
    for _ in range(10):
        for _replica in range(2):
            v = shared_state.consume("USR_A:write", limit=6, window_seconds=60)
            if not v.limited:
                allowed += 1
    assert allowed == 6, (
        f"{allowed} requests allowed against a limit of 6; the budget is not "
        f"shared between the two callers")


@requires_redis
def test_the_window_slides_rather_than_resetting(redis_state, monkeypatch):
    """A fixed window lets a caller send 2x the limit across the boundary. The
    in-process implementation always slid; this must too."""
    for _ in range(3):
        assert not shared_state.consume("slide", limit=3, window_seconds=1).limited
    assert shared_state.consume("slide", limit=3, window_seconds=1).limited

    import time
    time.sleep(1.2)
    assert not shared_state.consume("slide", limit=3, window_seconds=1).limited, (
        "the oldest hits aged out of the window and the slot was not released")


@requires_redis
def test_a_refusal_reports_when_to_come_back(redis_state):
    shared_state.consume("retry", limit=1, window_seconds=60)
    v = shared_state.consume("retry", limit=1, window_seconds=60)
    assert v.limited
    assert 1 <= v.retry_after_seconds <= 60


@requires_redis
def test_limits_are_per_principal_and_per_class(redis_state):
    for _ in range(3):
        shared_state.consume("USR_A:write", limit=3, window_seconds=60)
    assert shared_state.consume("USR_A:write", limit=3, window_seconds=60).limited
    # A different principal, and a different class of the same principal, are
    # different budgets. Sharing them would let a read flood block writes.
    assert not shared_state.consume("USR_B:write", limit=3, window_seconds=60).limited
    assert not shared_state.consume("USR_A:read", limit=3, window_seconds=60).limited


# --------------------------------------------------------------------------
# Degrading
# --------------------------------------------------------------------------
def test_without_redis_the_limiter_is_the_in_process_one(no_redis):
    assert shared_state.backend() == "process"
    assert shared_state.consume("x", limit=1, window_seconds=60) is None, (
        "no shared backend must return None so the caller falls back, rather "
        "than returning 'not limited' and removing the control")


def test_an_unreachable_redis_falls_back_rather_than_failing(monkeypatch):
    """A Redis blip must not become an API outage, and must not silently remove
    a security control either. It degrades to the single-process behaviour,
    which is a documented state."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")   # nothing there
    shared_state.reset_for_tests()
    try:
        assert shared_state.consume("x", limit=1, window_seconds=60) is None
        assert shared_state.backend() == "degraded", (
            "a configured Redis that is not answering must be visible, not "
            "indistinguishable from having none")
    finally:
        shared_state.reset_for_tests()


def test_the_route_still_limits_when_redis_is_gone(monkeypatch):
    """End to end through `check_rate_limit`: the per-process limiter takes over
    and still refuses. Losing Redis must not mean losing the limit."""
    from fastapi import HTTPException

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    shared_state.reset_for_tests()
    security.reset_rate_limits()
    try:
        limit = security.LIMITS["action"].requests
        for _ in range(limit):
            security.check_rate_limit("USR_DEGRADED", "/tasks/T/approve", "POST")
        with pytest.raises(HTTPException) as exc:
            security.check_rate_limit("USR_DEGRADED", "/tasks/T/approve", "POST")
        assert exc.value.status_code == 429
    finally:
        shared_state.reset_for_tests()
        security.reset_rate_limits()


# --------------------------------------------------------------------------
# The provider override
# --------------------------------------------------------------------------
@requires_redis
def test_the_provider_override_is_visible_to_another_replica(redis_state):
    from app import config

    assert config.set_runtime_llm_provider("deterministic") is True

    # A second replica has its own module global and has never been told.
    config._runtime_provider = None
    assert config.runtime_llm_provider() == "deterministic", (
        "the override did not reach a replica that did not serve the request")

    config.set_runtime_llm_provider(None)
    assert config.runtime_llm_provider() is None


def test_without_redis_the_override_reports_that_it_is_local(no_redis):
    from app import config

    assert config.set_runtime_llm_provider("deterministic") is False, (
        "the caller must be told the switch applied to one replica only")
    assert config.runtime_llm_provider() == "deterministic"
    config.set_runtime_llm_provider(None)


def test_an_unreachable_redis_does_not_read_as_a_cleared_override(monkeypatch):
    """The three-valued result earning its place. If UNAVAILABLE collapsed to
    None, a Redis outage would switch a fleet back to its configured provider in
    the middle of whatever the override was set for."""
    from app import config

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    shared_state.reset_for_tests()
    try:
        config._runtime_provider = "deterministic"
        assert config.runtime_llm_provider() == "deterministic"
    finally:
        config._runtime_provider = None
        shared_state.reset_for_tests()


def test_unavailable_is_not_none_and_is_falsy():
    assert shared_state.UNAVAILABLE is not None
    assert not shared_state.UNAVAILABLE
    assert repr(shared_state.UNAVAILABLE) == "UNAVAILABLE"
