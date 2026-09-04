"""Tokens that expire, rotate, and can be taken away.

Authentication was an HMAC of the user id: unforgeable, and valid forever. The
README has carried the limitation since the scheme was introduced -- "no expiry,
rotation, revocation list, or audience binding" -- and ADR-0048 sharpened it. A
leaver's token dies with their row now. A *leaked* token still worked until
somebody changed the server secret, which invalidates everybody's.

Four properties, and each of them is a different failure being closed:

  expiry       a token copied off a laptop stops working on its own
  revocation   a token known to be compromised stops working NOW
  rotation     the signing secret can be changed without a flag day
  binding      a token minted for something else is not accepted here

## The format

    mo1.<payload>.<signature>

`payload` is base64url JSON; `signature` is base64url HMAC-SHA256 over the exact
payload bytes. Self-describing and versioned, so a later scheme is a different
prefix rather than an ambiguity -- and readable with `base64 -d`, which matters
at three in the morning.

Not a JWT, deliberately. JWT's algorithm agility is its best-known
vulnerability class (`alg: none`, RS256-to-HS256 confusion), and none of what it
buys is needed here: there is one issuer, one verifier, and one algorithm. A
format with no algorithm field cannot be confused about the algorithm.

## Revocation has two shapes

**One token**, by `jti`, in `revoked_tokens`. What "sign out this session" and
"that laptop was stolen" need.

**Every token for a user**, by `users.credentials_valid_from`. A timestamp;
anything issued before it is refused. What "sign out everywhere" needs, and
what happens automatically when a refresh token is replayed. O(1), and it does
not grow a table per token.

## Refresh, and what happens when one is replayed

A refresh token is single-use. Presenting it returns a new access token AND a
new refresh token, and revokes the one presented. Presenting an already-revoked
refresh token means somebody has a copy of a token that has been used -- so
every token for that user is invalidated, not just that one. That is blunt, and
it is the right blunt: a replayed refresh token is either theft or a client bug,
and both are better resolved by everybody signing in again than by guessing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

PREFIX = "mo1"

#: The development fallback. Kept identical to the one `app/api/security.py`
#: has always used, so a laptop that worked before still works.
DEV_SECRET = "dev-only-insecure-secret"  # noqa: S105 - the placeholder itself

ACCESS = "access"
REFRESH = "refresh"


class TokenError(Exception):
    """A token that will not be honoured, and why."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------
def _keys() -> dict[str, bytes]:
    """Signing keys by id, newest first.

    `current` signs. `previous` only verifies, which is what makes rotation
    possible without a flag day: set `API_TOKEN_SECRET_PREVIOUS` to the old
    value, deploy, and every token already in the wild keeps working until it
    expires. Then drop it.

    The key id travels in the payload, so verification does not have to try each
    key and cannot be pushed into doing so by a caller.
    """
    keys = {"current": (os.environ.get("API_TOKEN_SECRET") or DEV_SECRET).encode()}
    previous = os.environ.get("API_TOKEN_SECRET_PREVIOUS")
    if previous:
        keys["previous"] = previous.encode()
    return keys


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: bytes, key: bytes) -> str:
    return _b64(hmac.new(key, payload, hashlib.sha256).digest())


# --------------------------------------------------------------------------
# Minting
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Claims:
    sub: str
    typ: str
    jti: str
    #: Seconds since the epoch, with microseconds. NOT whole seconds, and that
    #: is deliberate: `credentials_valid_from` refuses everything issued before
    #: a moment, and at one-second granularity a token minted in the same second
    #: as the reset is indistinguishable from one minted just before it. Either
    #: it survives a sign-out it should not have, or a legitimate sign-in in the
    #: same second is refused. JWT's convention is whole seconds; this is not a
    #: JWT, and the ambiguity buys nothing.
    iat: float
    exp: float
    kid: str = "current"

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, UTC)


def mint(user_id: str, *, typ: str = ACCESS, lifetime_seconds: int | None = None,
         now: datetime | None = None) -> str:
    from app.config import get_settings

    settings = get_settings()
    if lifetime_seconds is None:
        lifetime_seconds = (settings.auth_refresh_token_seconds if typ == REFRESH
                            else settings.auth_access_token_seconds)

    issued = now or datetime.now(UTC)
    payload = {
        "sub": user_id,
        "typ": typ,
        # A unique id per token, so one can be revoked without revoking the
        # user. Without it, "revoke this token" has nothing to name.
        "jti": uuid.uuid4().hex,
        "iat": issued.timestamp(),
        "exp": (issued + timedelta(seconds=lifetime_seconds)).timestamp(),
        "kid": "current",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{PREFIX}.{_b64(raw)}.{_sign(raw, _keys()['current'])}"


# --------------------------------------------------------------------------
# Verifying
# --------------------------------------------------------------------------
def parse(token: str, *, expect: str = ACCESS,
          now: datetime | None = None) -> Claims:
    """Authenticity, then shape, then time. Raises `TokenError` with a code.

    Signature first and always: everything after it reads attacker-controlled
    JSON, and checking `exp` on a payload nobody has authenticated is checking a
    number the caller chose.
    """
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenError("Not a MerchantOps token.", "malformed")

    _, body, signature = parts
    try:
        raw = _unb64(body)
        payload = json.loads(raw)
    except Exception as exc:
        raise TokenError("Token payload is not readable.", "malformed") from exc

    kid = payload.get("kid", "current")
    key = _keys().get(kid)
    if key is None:
        # Signed with a key this process no longer has. During a rotation that
        # means the overlap window closed; it is not an invalid signature and
        # saying so distinguishes "rotate again" from "someone is forging".
        raise TokenError("Token was signed with a key this server no longer "
                         "holds. Sign in again.", "unknown_key")

    # Constant time: a short-circuiting comparison leaks the signature byte by
    # byte, and a forged token is a valid identity.
    if not hmac.compare_digest(signature, _sign(raw, key)):
        raise TokenError("Signature does not verify.", "bad_signature")

    for field in ("sub", "typ", "jti", "iat", "exp"):
        if field not in payload:
            raise TokenError(f"Token is missing {field}.", "malformed")

    if payload["typ"] != expect:
        # An access token presented to the refresh endpoint, or the reverse.
        # Both are the same bytes to a signature check and mean different
        # things, which is exactly what `typ` exists to keep apart.
        raise TokenError(f"This is a {payload['typ']} token; a {expect} token "
                         f"is required here.", "wrong_type")

    moment = now or datetime.now(UTC)
    if float(payload["exp"]) <= moment.timestamp():
        raise TokenError("Token has expired.", "expired")

    return Claims(sub=payload["sub"], typ=payload["typ"], jti=payload["jti"],
                  iat=float(payload["iat"]), exp=float(payload["exp"]), kid=kid)


def check_not_revoked(session, claims: Claims) -> None:
    """The database's say, after the signature's.

    Two questions, because they answer different needs: was this ONE token taken
    away, and was EVERY token for this user taken away. The second is a
    timestamp rather than a row per token, so signing out everywhere costs one
    update instead of one insert per live session.
    """
    revoked = session.execute(text(
        "SELECT reason FROM revoked_tokens WHERE jti = :j"), {"j": claims.jti}).scalar()
    if revoked:
        raise TokenError(f"Token was revoked ({revoked}).", "revoked")

    valid_from = session.execute(text(
        "SELECT credentials_valid_from FROM users WHERE id = :u"),
        {"u": claims.sub}).scalar()
    if valid_from is not None:
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=UTC)
        # Both sides carry microseconds, so this is an exact comparison rather
        # than a choice about which way to round. At whole-second granularity it
        # was neither: a token minted in the same second as the reset either
        # survived a sign-out it should not have, or a legitimate sign-in one
        # second later was refused.
        if claims.iat < valid_from.timestamp():
            raise TokenError(
                "Token was issued before this account's credentials were reset.",
                "superseded")


# --------------------------------------------------------------------------
# Revoking
# --------------------------------------------------------------------------
def revoke(session, claims: Claims, *, reason: str) -> None:
    """Take one token away. Idempotent."""
    session.execute(text("""
        INSERT INTO revoked_tokens (jti, user_id, reason, revoked_at, expires_at)
        VALUES (:j, :u, :r, now(), :e)
        ON CONFLICT (jti) DO NOTHING
    """), {"j": claims.jti, "u": claims.sub, "r": reason,
           "e": claims.expires_at})
    session.flush()


def revoke_all_for(session, user_id: str, *, now: datetime | None = None) -> None:
    """Take every token for a user away, including ones not yet seen.

    A timestamp, not a sweep: there is no list of live tokens to walk, because
    the whole point of a self-contained token is that the server does not keep
    one.
    """
    session.execute(text(
        "UPDATE users SET credentials_valid_from = :t WHERE id = :u"),
        {"t": now or datetime.now(UTC), "u": user_id})
    session.flush()


def prune_revoked(session, *, now: datetime | None = None) -> int:
    """Drop revocations for tokens that have expired anyway.

    A denylist that only grows is a denylist that eventually costs more than the
    thing it protects. Once a token's own `exp` has passed, `parse` refuses it
    without consulting this table at all.
    """
    result = session.execute(text(
        "DELETE FROM revoked_tokens WHERE expires_at < :t"),
        {"t": now or datetime.now(UTC)})
    session.flush()
    return result.rowcount or 0


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Issued:
    access_token: str
    refresh_token: str
    expires_in: int


def issue_pair(user_id: str) -> Issued:
    from app.config import get_settings

    return Issued(
        access_token=mint(user_id, typ=ACCESS),
        refresh_token=mint(user_id, typ=REFRESH),
        expires_in=get_settings().auth_access_token_seconds,
    )


def refresh(session, refresh_token: str, *, now: datetime | None = None) -> Issued:
    """Exchange a refresh token for a new pair, and burn the one presented.

    Replaying a refresh token invalidates EVERY token for that user, not just
    the one replayed. A refresh token is single-use by construction, so a second
    presentation means somebody else has a copy -- theft or a client bug, and
    both are better resolved by everybody signing in again than by guessing
    which of the two holders is legitimate.
    """
    claims = parse(refresh_token, expect=REFRESH, now=now)

    try:
        check_not_revoked(session, claims)
    except TokenError as exc:
        if exc.code == "revoked":
            revoke_all_for(session, claims.sub, now=now)
            # COMMITTED before raising, and this is the whole point of the
            # detection. The caller turns this exception into a 401, the 401
            # leaves `session_scope`, and `session_scope` rolls back -- so
            # without this the security response to a replayed token would be
            # undone by the act of reporting it. Every session would stay live
            # and the log would say they had been closed.
            #
            # `checkpoint` is the same primitive ADR-0029 introduced for the
            # action record, for the same reason: some writes must outlive the
            # failure that follows them.
            from app.db import checkpoint

            checkpoint(session)
            raise TokenError(
                "This refresh token has already been used. Every session for "
                "this account has been signed out as a precaution.",
                "replayed") from exc
        raise

    revoke(session, claims, reason="rotated")
    return issue_pair(claims.sub)
