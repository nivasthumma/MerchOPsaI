"""API authentication and rate limiting — closes two threat-model residual risks.

## Authentication

Previously the caller asserted its own identity with an `X-User-Id` header, which
means anyone who can reach the API is any user they name. The principal was at
least resolved server-side from the database, but nothing verified the claim.

This replaces it with a bearer token the caller cannot forge: an HMAC-SHA256 of
the user id under a server-held secret, compared in constant time. That is not a
production identity provider — there is no expiry, rotation, revocation list, or
audience binding — but it does close the "any caller is any user" hole, and it
makes the trust boundary explicit rather than aspirational.

`scripts/issue_token.py` mints tokens for local use.

## Rate limiting

A **sliding window log** per (principal, route class): the timestamps of recent
requests, with the ones older than the window dropped on each check. Agent tasks
are expensive — each one runs a model loop and several database queries — so an
unauthenticated flood was previously bounded only by the box. Write and action
routes get tighter limits than reads.

(This docstring described a *fixed* window for a long time. The code never was
one — it has always kept timestamps and filtered by a cutoff. A fixed window
would let a caller send twice the limit across a boundary, so the code was the
better of the two; only the description was wrong.)

Shared across replicas when `REDIS_URL` is set (`app/shared_state.py`), and
per-process when it is not. Per-process is exact with one worker and multiplies
by N with N of them — and on a serverless host, where every invocation may be a
new process, it is not a limit at all. `/health` reports which is live, so the
two are never a guess.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app import shared_state, tenancy
from app.agent.runtime import Principal
from app.db import session_scope
from app.observability.logs import get_logger

log = get_logger("merchantops.security")


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
DEV_SECRET = "dev-only-insecure-secret"  # noqa: S105 - the placeholder itself


def _secret() -> bytes:
    """Signing secret. Falls back to a fixed development value so the project
    runs out of the box; `require_configured_secret` refuses that fallback
    anywhere that is not a developer's machine."""
    return (os.environ.get("API_TOKEN_SECRET") or DEV_SECRET).encode()


DEV_SECRET_IN_USE = os.environ.get("API_TOKEN_SECRET") is None


class InsecureConfiguration(RuntimeError):
    """The process is configured in a way that cannot be allowed to serve."""


# Environment variables set by the platform, not by us. Their presence means
# this process is not a laptop, whatever anyone intended.
_DEPLOYMENT_MARKERS = ("VERCEL", "AWS_EXECUTION_ENV", "KUBERNETES_SERVICE_HOST",
                       "DYNO", "RENDER", "FLY_APP_NAME", "WEBSITE_INSTANCE_ID")


def deployment_context() -> str | None:
    """Which marker says this is a deployment, or None if nothing does."""
    if os.environ.get("MERCHANTOPS_ENV", "").lower() in ("production", "staging", "prod"):
        return "MERCHANTOPS_ENV"
    return next((m for m in _DEPLOYMENT_MARKERS if os.environ.get(m)), None)


def require_configured_secret() -> None:
    """Refuse to serve on a deployment that is signing tokens with the default.

    The fallback in `_secret` is a real convenience -- `make api` works on a
    fresh clone with no configuration -- and a real hazard, because the value is
    in this file. Anyone who can read the repository can mint a token for any
    user, and permissions are then read from the database exactly as they would
    be for a genuine one. The checks behind the token are sound; the identity in
    front of them is a formality.

    `/health` has reported this since the token scheme was introduced, and
    reporting is not a control: nothing consults the report before serving. The
    Vercel environment file is the case in point -- seventeen database variables
    and no API_TOKEN_SECRET.

    So the default is refused where a platform marker says this is a deployment,
    and permitted where nothing does. That keeps local development frictionless,
    which is the only reason the fallback exists. Set
    MERCHANTOPS_ALLOW_DEV_SECRET=1 to override deliberately -- for a throwaway
    preview, say -- so that running with it is a decision somebody made rather
    than one nobody noticed.
    """
    if not DEV_SECRET_IN_USE:
        return
    if os.environ.get("MERCHANTOPS_ALLOW_DEV_SECRET"):
        return
    marker = deployment_context()
    if marker is None:
        return

    raise InsecureConfiguration(
        f"Refusing to start: API_TOKEN_SECRET is unset, so bearer tokens would "
        f"be signed with the development default that is published in this "
        f"repository's source. {marker} is set, so this is a deployment rather "
        f"than a development machine.\n\n"
        f"Anyone who can read the repository could mint a token for any user.\n\n"
        f"Set a real one:\n"
        f"    API_TOKEN_SECRET=$(python -c \"import secrets; "
        f"print(secrets.token_urlsafe(48))\")\n\n"
        f"Or set MERCHANTOPS_ALLOW_DEV_SECRET=1 to accept the risk explicitly."
    )


def issue_token(user_id: str) -> str:
    sig = hmac.new(_secret(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def verify_token(token: str) -> str | None:
    """Return the user id if the token is authentic, else None."""
    if not token or "." not in token:
        return None
    user_id, _, sig = token.rpartition(".")
    if not user_id or not sig:
        return None
    expected = hmac.new(_secret(), user_id.encode(), hashlib.sha256).hexdigest()
    # Constant time: a short-circuiting comparison leaks the signature byte by byte.
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


LIMITS: dict[str, Limit] = {
    "read": Limit(120, 60),      # status, trace, listings
    "write": Limit(20, 60),      # creating agent tasks — each runs a model loop
    "action": Limit(10, 60),     # approve / reject / reverify / reconcile
    # Webhooks are unauthenticated by necessity: the provider holds no bearer
    # token, so the signature is the gate. The ceiling is high because providers
    # legitimately burst and retry, and low enough to bound an unsigned flood --
    # rejected deliveries still cost a database write, which is the point of
    # storing them and also the reason to cap them.
    "webhook": Limit(300, 60),
}

_hits: dict[tuple[str, str], list[float]] = defaultdict(list)


def _class_for(path: str, method: str) -> str:
    if path.startswith("/webhooks/"):
        return "webhook"
    if any(p in path for p in ("/approve", "/reject", "/reverify", "/reconcile")):
        return "action"
    if method == "POST":
        return "write"
    return "read"


def check_rate_limit(principal_id: str, path: str, method: str) -> None:
    cls = _class_for(path, method)
    limit = LIMITS[cls]

    # Shared first. Returns None when there is no Redis configured, and also
    # when a configured one is unreachable -- in both cases the per-process
    # limiter below runs instead, which is a documented state rather than no
    # limit at all.
    verdict = shared_state.consume(
        f"{principal_id}:{cls}", limit=limit.requests,
        window_seconds=limit.window_seconds)
    if verdict is not None:
        if verdict.limited:
            _refuse(cls, limit, verdict.retry_after_seconds)
        return

    _check_in_process(principal_id, cls, limit)


def _refuse(cls: str, limit: Limit, retry: int) -> None:
    raise HTTPException(
        429,
        detail={"error": "rate_limit_exceeded", "limit_class": cls,
                "limit": f"{limit.requests}/{limit.window_seconds}s",
                "retry_after_seconds": retry},
        headers={"Retry-After": str(retry)},
    )


def _check_in_process(principal_id: str, cls: str, limit: Limit) -> None:
    key = (principal_id, cls)
    now = time.monotonic()

    window = _hits[key]
    cutoff = now - limit.window_seconds
    # Drop expired hits in place so the list cannot grow without bound.
    window[:] = [t for t in window if t > cutoff]

    if len(window) >= limit.requests:
        _refuse(cls, limit, int(limit.window_seconds - (now - window[0])) + 1)
    window.append(now)


def reset_rate_limits() -> None:
    """Test hook. Rate limit state must not leak between tests -- both copies of
    it, since a suite run with REDIS_URL set uses the shared one."""
    _hits.clear()
    client = shared_state.get_client()
    if client is not None:
        try:
            for key in client.scan_iter("rl:*", count=500):
                client.delete(key)
        except Exception as exc:
            # A test hook that fails because Redis is down should not fail the
            # test that called it; the process-local clear above still happened.
            # Logged rather than swallowed, because a suite that silently stops
            # clearing shared state produces failures in whichever test runs
            # next, which is the hardest kind to attribute.
            log.warning("rate_limit_reset_incomplete", extra={
                "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------
# Dependency
# --------------------------------------------------------------------------
async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Authenticate, rate limit, and bind the principal to this request.

    Async, and that is load-bearing rather than stylistic. FastAPI runs a *sync*
    dependency and a *sync* endpoint in two different threadpool contexts, each
    a copy of the request's -- so a ContextVar set in a sync dependency is
    invisible to the endpoint, and the row-level-security binding silently did
    nothing. Found by a test that removed the application's own merchant check
    and watched the request succeed anyway.

    Awaited here, the binding happens in the request's own task context, and the
    endpoint's threadpool run copies it. The blocking work stays off the event
    loop in `run_in_threadpool` below, which is where it was already.
    """
    principal = await run_in_threadpool(_authenticate, request, authorization)

    # Bind for row-level security (ADR-0046). Every `session_scope` opened from
    # here on pushes this onto its transaction, so a query that forgets
    # `WHERE merchant_id` returns nothing rather than another merchant's rows.
    #
    # Not reset afterwards, and it does not need to be: this context belongs to
    # this request, and the next one starts from the default.
    tenancy.bind(principal.tenant_id, principal.merchant_id)
    return principal


def _authenticate(request: Request, authorization: str | None) -> Principal:
    """Authenticate, then rate limit. Order matters: rate limiting an
    unauthenticated caller by a self-declared identity would let an attacker
    exhaust someone else's budget."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            401, "Missing bearer token. Mint one with scripts/issue_token.py.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = verify_token(authorization[7:].strip())
    if user_id is None:
        raise HTTPException(401, "Invalid token.",
                            headers={"WWW-Authenticate": "Bearer"})

    with session_scope() as s:
        row = s.execute(text("""
            SELECT id, tenant_id, merchant_id, role, permissions FROM users WHERE id = :u
        """), {"u": user_id}).mappings().first()
    if row is None:
        # The token is authentic but the subject no longer exists.
        raise HTTPException(401, "Unknown principal.")

    check_rate_limit(row["id"], request.url.path, request.method)

    # Permissions are read from the database on every request, never from the
    # token. A token therefore cannot carry stale or elevated authority.
    return Principal(row["tenant_id"], row["id"], row["merchant_id"],
                     row["role"], list(row["permissions"]))
