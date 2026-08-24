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

A fixed-window counter per (principal, route class), in-process. Agent tasks are
expensive — each one runs a model loop and several database queries — so an
unauthenticated flood was previously bounded only by the box. Write and action
routes get tighter limits than reads.

In-process state means the limit is per-worker. With one worker that is exact;
with several it is approximate. A shared counter needs Redis, which §52 excludes,
so the limitation is stated rather than hidden.
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

from app.agent.runtime import Principal
from app.db import session_scope


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def _secret() -> bytes:
    """Signing secret. Falls back to a fixed development value so the project
    runs out of the box; the API refuses to start in strict mode without a real
    one (see require_configured_secret)."""
    return (os.environ.get("API_TOKEN_SECRET") or "dev-only-insecure-secret").encode()


DEV_SECRET_IN_USE = os.environ.get("API_TOKEN_SECRET") is None


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
}

_hits: dict[tuple[str, str], list[float]] = defaultdict(list)


def _class_for(path: str, method: str) -> str:
    if any(p in path for p in ("/approve", "/reject", "/reverify", "/reconcile")):
        return "action"
    if method == "POST":
        return "write"
    return "read"


def check_rate_limit(principal_id: str, path: str, method: str) -> None:
    cls = _class_for(path, method)
    limit = LIMITS[cls]
    key = (principal_id, cls)
    now = time.monotonic()

    window = _hits[key]
    cutoff = now - limit.window_seconds
    # Drop expired hits in place so the list cannot grow without bound.
    window[:] = [t for t in window if t > cutoff]

    if len(window) >= limit.requests:
        retry = int(limit.window_seconds - (now - window[0])) + 1
        raise HTTPException(
            429,
            detail={"error": "rate_limit_exceeded", "limit_class": cls,
                    "limit": f"{limit.requests}/{limit.window_seconds}s",
                    "retry_after_seconds": retry},
            headers={"Retry-After": str(retry)},
        )
    window.append(now)


def reset_rate_limits() -> None:
    """Test hook. Rate limit state is process-local and must not leak between tests."""
    _hits.clear()


# --------------------------------------------------------------------------
# Dependency
# --------------------------------------------------------------------------
def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
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
            SELECT id, merchant_id, role, permissions FROM users WHERE id = :u
        """), {"u": user_id}).mappings().first()
    if row is None:
        # The token is authentic but the subject no longer exists.
        raise HTTPException(401, "Unknown principal.")

    check_rate_limit(row["id"], request.url.path, request.method)

    # Permissions are read from the database on every request, never from the
    # token. A token therefore cannot carry stale or elevated authority.
    return Principal(row["id"], row["merchant_id"], row["role"], list(row["permissions"]))
