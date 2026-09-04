"""State that has to be the same in every replica, and what happens when it cannot be.

Three things lived in process memory and one of them mattered.

**The rate-limit counter** is a security control. With one worker it is exact;
with several it is per-worker, so N replicas serve N times the configured limit.
On Vercel it is worse than approximate -- every invocation is its own process,
so the counter starts empty on most requests and the limit is not enforced at
all. That is the one this module exists for.

**The runtime provider override** (`POST /config/llm-provider`) is a live switch
that, held in a module global, applied to whichever replica happened to serve
the request. Somebody switching to the deterministic planner would switch it for
one of three replicas and see the model still being used.

**The credential-detection cache** is `@lru_cache` over a probe of this
process's own environment. Every replica has the same environment and would
reach the same answer, and changing that environment means a restart regardless.
It is process-local and correct; naming it alongside the other two, as an
earlier review did, overstated it. It stays where it is.

## Degrading, not failing

`REDIS_URL` unset is a supported configuration, not a broken one -- a laptop and
a single-container stack are both single-process, where the in-process
implementation is exact. `/health` reports which backend is live, so "shared" and
"per-process" are never a guess.

When Redis is *configured* and then becomes unreachable, this falls back to the
in-process implementation for that call and logs it, rather than failing the
request. The alternatives are both worse: failing closed turns a Redis blip into
an outage of the whole API, and failing open removes the control silently. The
fallback degrades to exactly the behaviour of the deployment that has no Redis,
which is a known and documented state rather than a new one.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from app.observability.logs import get_logger

log = get_logger("merchantops.shared_state")

_client = None
_client_built = False
_lock = threading.Lock()

#: Set when a configured Redis has failed. Read by `/health`, so a deployment
#: that believes it is sharing state can find out that it is not.
_degraded_since: float | None = None
_last_degraded_log: float = 0.0


def redis_url() -> str | None:
    return os.environ.get("REDIS_URL") or None


def get_client():
    """The Redis client, or None when there is no `REDIS_URL`.

    Built once. `socket_timeout` is deliberately short: this sits in front of
    every authenticated request, and a Redis that has stopped answering must
    cost milliseconds and fall back, not hold a request open.
    """
    global _client, _client_built
    if _client_built:
        return _client
    with _lock:
        if _client_built:
            return _client
        url = redis_url()
        if url:
            import redis

            _client = redis.Redis.from_url(
                url, socket_timeout=0.25, socket_connect_timeout=0.25,
                health_check_interval=30, decode_responses=True)
        _client_built = True
    return _client


def _degrade(where: str, exc: Exception) -> None:
    """Record that configured Redis is not answering, without flooding the log."""
    global _degraded_since, _last_degraded_log
    now = time.monotonic()
    if _degraded_since is None:
        _degraded_since = now
    if now - _last_degraded_log > 30:
        _last_degraded_log = now
        log.warning("shared_state_degraded", extra={"shared_state": {
            "where": where, "error": f"{type(exc).__name__}: {exc}",
            "effect": "falling back to per-process state for this call"}})


def _recovered() -> None:
    global _degraded_since
    if _degraded_since is not None:
        _degraded_since = None
        log.info("shared_state_recovered", extra={"shared_state": {}})


def backend() -> str:
    """`shared`, `degraded`, or `process`. Reported by /health."""
    if get_client() is None:
        return "process"
    return "degraded" if _degraded_since is not None else "shared"


def reset_for_tests() -> None:
    global _client, _client_built, _degraded_since, _last_degraded_log, _script
    _client = None
    _client_built = False
    _degraded_since = None
    _last_degraded_log = 0.0
    # The registered script holds the client it was registered against. Leaving
    # it behind means the next `consume` runs against the OLD connection --
    # which in a test that repoints REDIS_URL is the previous test's Redis, and
    # the symptom is a rate limit that appears to work while pointed at nothing.
    _script = None


# --------------------------------------------------------------------------
# Sliding-window rate limiting
# --------------------------------------------------------------------------
#: A sliding window log in a sorted set, applied atomically.
#:
#: The window is *sliding*, not fixed, because that is what the in-process
#: implementation has always done -- it keeps timestamps and drops the ones
#: older than the cutoff. (The module docstring in `app/api/security.py` called
#: it a fixed window for a long time; the code never was one.) Preserved rather
#: than simplified, because a fixed window lets a caller send 2x the limit
#: across a boundary, and changing a security control's behaviour while moving
#: where it is stored is two changes wearing one commit.
#:
#: `TIME` is read inside the script rather than passed in. A timestamp from the
#: caller is that replica's clock, and the whole point of this is that several
#: replicas share one window; two servers a second apart would otherwise
#: disagree about what "the last sixty seconds" contains. Redis's clock is the
#: only one all of them can see.
_SLIDING_WINDOW = """
local now_pair = redis.call('TIME')
local now_ms = (tonumber(now_pair[1]) * 1000) + math.floor(tonumber(now_pair[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - window_ms)
local used = redis.call('ZCARD', KEYS[1])

if used >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_ms = window_ms - (now_ms - tonumber(oldest[2]))
  return {1, retry_ms}
end

redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
-- Expire the key one window after the newest hit, so an idle caller's key is
-- reclaimed instead of living forever.
redis.call('PEXPIRE', KEYS[1], window_ms)
return {0, 0}
"""

_script = None


@dataclass(frozen=True)
class Verdict:
    limited: bool
    retry_after_seconds: int


def consume(key: str, *, limit: int, window_seconds: int) -> Verdict | None:
    """Take one slot against a shared window.

    Returns None when there is no shared backend, or when a configured one
    failed -- the caller then applies its own per-process limiter, which is the
    documented single-process behaviour rather than no limit at all.
    """
    global _script
    client = get_client()
    if client is None:
        return None

    import uuid

    try:
        if _script is None:
            _script = client.register_script(_SLIDING_WINDOW)
        limited, retry_ms = _script(
            keys=[f"rl:{key}"],
            args=[window_seconds * 1000, limit, uuid.uuid4().hex])
        _recovered()
    except Exception as exc:
        # Includes the case where the script cache was flushed under us --
        # `register_script` re-uploads on NOSCRIPT, but a connection error
        # during that is the same fallback.
        _script = None
        _degrade("rate_limit", exc)
        return None

    return Verdict(bool(limited), max(1, -(-int(retry_ms) // 1000)))


# --------------------------------------------------------------------------
# The runtime provider override
# --------------------------------------------------------------------------
_OVERRIDE_KEY = "merchantops:llm_provider_override"

#: Long enough to be a working session, short enough that a switch somebody
#: made and forgot does not outlive the reason for it. The in-process version
#: expired on restart, which was a bound of a different kind; this keeps one.
_OVERRIDE_TTL_SECONDS = 12 * 60 * 60


def get_provider_override() -> str | object | None:
    """The shared override, or `UNAVAILABLE` when there is no shared backend.

    Three-valued on purpose. `None` means "no override is set", which is a real
    answer; a caller that cannot tell it apart from "I could not ask" would
    silently treat a Redis outage as somebody having cleared the setting.
    """
    client = get_client()
    if client is None:
        return UNAVAILABLE
    try:
        value = client.get(_OVERRIDE_KEY)
        _recovered()
    except Exception as exc:
        _degrade("provider_override_get", exc)
        return UNAVAILABLE
    return value or None


def set_provider_override(value: str | None) -> bool:
    """True if it was stored where every replica can see it."""
    client = get_client()
    if client is None:
        return False
    try:
        if value is None:
            client.delete(_OVERRIDE_KEY)
        else:
            client.set(_OVERRIDE_KEY, value, ex=_OVERRIDE_TTL_SECONDS)
        _recovered()
    except Exception as exc:
        _degrade("provider_override_set", exc)
        return False
    return True


class _Unavailable:
    """Distinct from None. See `get_provider_override`."""

    def __repr__(self) -> str:
        return "UNAVAILABLE"

    def __bool__(self) -> bool:
        return False


UNAVAILABLE = _Unavailable()
