"""Signing in through the customer's identity provider — OIDC authorization code flow.

Every enterprise security review asks for this before it asks anything else, and
until now the answer was a bearer token an engineer minted by hand.

## The flow, and what each piece defends

    /auth/sso/start     ->  302 to the IdP, carrying state, nonce, PKCE challenge
    (the customer authenticates at their own provider)
    /auth/sso/callback  <-  code + state
                            state    proves the callback answers OUR redirect
                            code     exchanged server-to-server for an ID token
                            nonce    proves the ID token answers THIS attempt
                            verifier proves the code was redeemed by whoever
                                     started the flow, not by whoever intercepted it
    /auth/sso/exchange  ->  the handoff code becomes a MerchantOps token pair

The handoff code exists so that no credential ever travels in a URL. A token in
a fragment or query lands in browser history, in the referrer of whatever the
page loads next, and in every proxy log along the way. A single-use code that
must be POSTed back does not.

## The ID token's signature is not verified, and that is deliberate

OIDC Core §3.1.3.7 clause 6: *"If the ID Token is received via direct
communication between the Client and the Token Endpoint (which it is in this
flow), the TLS server validation MAY be used to validate the issuer in place of
checking the token signature."*

That is this flow. The ID token is not accepted from the browser; it is fetched
by this server, from the discovered `token_endpoint`, over TLS whose certificate
chain is validated, authenticating with the client secret. Nothing between the
provider and this process could have substituted it without also breaking TLS.

Verifying the signature anyway would mean fetching and caching a JWKS, selecting
by `kid`, and implementing RS256 verification -- a native cryptography
dependency and a well-populated family of JWS bugs (algorithm confusion,
unverified `kid` fetching, key substitution) bought to re-check something TLS has
already established.

**This reasoning does NOT extend to an ID token arriving any other way.** In the
implicit or hybrid flows the token comes through the browser and the signature is
the only thing standing between an attacker and an identity. Those flows are not
implemented, and adding one means adding signature verification first.

Everything else in the token IS checked, because none of it follows from TLS:
`iss` exactly, `aud` against our client id, `exp` against the clock, and `nonce`
against the value this flow generated.

## Just-in-time provisioning

A user arriving for the first time is created with the provider's
`default_role`. Never `owner` -- an identity provider deciding who administers a
tenant means anybody who can create an account at the customer's IdP can
administer their MerchantOps. Refused at configuration time and again here.

A DISABLED user who signs in successfully stays disabled. Offboarding is a
decision this system made about a person, and their IdP still recognising them
is not new information.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from app.observability.logs import get_logger

log = get_logger("merchantops.sso")

#: How long a sign-in attempt may stay open. Long enough to type a password and
#: answer an MFA prompt, short enough that an abandoned flow is not a row
#: somebody can come back to tomorrow.
FLOW_TTL_SECONDS = 600

#: How long the handoff code is worth. It is exchanged by the page the browser
#: was just redirected to, so this is seconds of work, not minutes.
HANDOFF_TTL_SECONDS = 120

_DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}


class SsoError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def discover(issuer: str, *, timeout: float = 5.0) -> dict:
    """The provider's endpoints, from its well-known document.

    Cached for an hour. Providers rotate endpoints approximately never, and a
    fetch on every sign-in makes the customer's IdP a hard dependency of every
    login rather than of the first one each hour.
    """
    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached and now - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        raise SsoError(f"Could not read {url}: {type(exc).__name__}.",
                       "discovery_failed") from exc

    # The issuer in the document must be the issuer we asked about. A provider
    # that answers for somebody else is either misconfigured or hostile, and the
    # difference does not matter here.
    if document.get("issuer") != issuer:
        raise SsoError(
            f"{url} declares issuer {document.get('issuer')!r}, not {issuer!r}.",
            "issuer_mismatch")
    for field in ("authorization_endpoint", "token_endpoint"):
        if not document.get(field):
            raise SsoError(f"{url} has no {field}.", "discovery_incomplete")

    _discovery_cache[issuer] = (now, document)
    return document


def forget_discovery() -> None:
    """Test hook, and the thing to call after a provider is reconfigured."""
    _discovery_cache.clear()


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class Started:
    authorization_url: str
    state: str


_SAFE_REDIRECT = re.compile(r"^/[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#\[\]]*$")


def _safe_redirect(target: str | None) -> str:
    """A path on this site, or `/`.

    An open redirect on a login endpoint is how a phishing page borrows somebody
    else's domain: the victim sees a legitimate host, signs in, and is handed to
    the attacker. Anything that is not a same-site absolute path is discarded
    rather than sanitised -- `//evil.example` is a protocol-relative URL and
    looks like a path.
    """
    if not target or not _SAFE_REDIRECT.match(target) or target.startswith("//"):
        return "/"
    return target


def provider_for_email(session, email: str) -> dict | None:
    """Which tenant's provider owns this address, by domain."""
    domain = (email or "").strip().lower().rpartition("@")[2]
    if not domain:
        return None
    row = session.execute(text("""
        SELECT * FROM identity_providers
        WHERE enabled AND email_domains::jsonb ? :d
    """), {"d": domain}).mappings().first()
    return dict(row) if row else None


def start(session, *, email: str, redirect_uri: str,
          redirect_to: str | None = None) -> Started:
    provider = provider_for_email(session, email)
    if provider is None:
        # Deliberately the same answer for "no provider configured" and "domain
        # we have never heard of". Distinguishing them tells an unauthenticated
        # caller which companies are customers.
        raise SsoError("No identity provider is configured for that address.",
                       "no_provider")

    document = discover(provider["issuer"])

    state = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())

    session.execute(text("""
        INSERT INTO sso_flows (state, tenant_id, nonce, code_verifier,
                               redirect_to, created_at, expires_at)
        VALUES (:s, :t, :n, :v, :r, now(), :e)
    """), {"s": state, "t": provider["tenant_id"], "n": nonce, "v": verifier,
           "r": _safe_redirect(redirect_to),
           "e": datetime.now(UTC) + timedelta(seconds=FLOW_TTL_SECONDS)})
    session.flush()

    query = urlencode({
        "response_type": "code",
        "client_id": provider["client_id"],
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        # S256, never `plain`. `plain` sends the verifier itself in the
        # authorization request, which is the thing PKCE exists to avoid.
        "code_challenge_method": "S256",
        # The address the user typed, so their provider can skip asking again.
        # A hint, not an assertion -- the ID token is what says who they are.
        "login_hint": email,
    })
    return Started(f"{document['authorization_endpoint']}?{query}", state)


# --------------------------------------------------------------------------
# Coming back
# --------------------------------------------------------------------------
def _claims_from(id_token: str) -> dict:
    """The ID token's payload, unverified by signature. See the module docstring.

    Read without checking the JWS because this token arrived over an
    authenticated TLS channel from the token endpoint (OIDC Core §3.1.3.7), and
    NOT because reading it is safe in general. Every claim it carries is checked
    by the caller.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise SsoError("The provider returned a malformed ID token.", "bad_id_token")
    try:
        body = parts[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception as exc:
        raise SsoError("The provider's ID token is not readable.",
                       "bad_id_token") from exc


@dataclass(frozen=True)
class Completed:
    user_id: str
    email: str
    handoff_code: str
    redirect_to: str
    created: bool


def complete(session, *, state: str, code: str, redirect_uri: str,
             now: datetime | None = None) -> Completed:
    moment = now or datetime.now(UTC)

    flow = session.execute(text("""
        SELECT * FROM sso_flows WHERE state = :s FOR UPDATE
    """), {"s": state}).mappings().first()
    if flow is None:
        raise SsoError("This sign-in did not start here.", "unknown_state")
    if flow["consumed_at"] is not None:
        # A replayed callback. The code has already been exchanged, so either
        # the browser retried or somebody captured the redirect.
        raise SsoError("This sign-in has already been completed.", "state_reused")
    expires = flow["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < moment:
        raise SsoError("This sign-in took too long. Start again.", "expired")

    provider = session.execute(text(
        "SELECT * FROM identity_providers WHERE tenant_id = :t"),
        {"t": flow["tenant_id"]}).mappings().first()
    if provider is None or not provider["enabled"]:
        raise SsoError("That identity provider is no longer configured.",
                       "no_provider")

    document = discover(provider["issuer"])

    # Server to server, over TLS, authenticated with the client secret. This is
    # the channel the signature check is being traded against.
    try:
        response = httpx.post(document["token_endpoint"], timeout=10.0, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider["client_id"],
            "client_secret": provider["client_secret"],
            "code_verifier": flow["code_verifier"],
        }, headers={"Accept": "application/json"})
    except Exception as exc:
        raise SsoError(f"Could not reach the token endpoint: "
                       f"{type(exc).__name__}.", "token_endpoint_unreachable") from exc

    if response.status_code != 200:
        # The provider's own error, not ours. Logged with its body because
        # `invalid_grant` and `invalid_client` need different people.
        log.warning("sso_token_exchange_failed", extra={"sso": {
            "tenant_id": flow["tenant_id"], "status": response.status_code,
            "body": response.text[:400]}})
        raise SsoError("The identity provider refused the authorization code.",
                       "token_exchange_failed")

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise SsoError("The provider returned no ID token.", "no_id_token")

    claims = _claims_from(id_token)

    # Everything TLS does not establish.
    if claims.get("iss") != provider["issuer"]:
        raise SsoError(f"ID token issuer {claims.get('iss')!r} is not "
                       f"{provider['issuer']!r}.", "issuer_mismatch")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if provider["client_id"] not in audiences:
        raise SsoError("ID token was issued for a different client.",
                       "audience_mismatch")
    if claims.get("nonce") != flow["nonce"]:
        # Binds the token to THIS attempt. Without it a token obtained in one
        # sign-in could be replayed into another.
        raise SsoError("ID token does not answer this sign-in.", "nonce_mismatch")
    if float(claims.get("exp", 0)) <= moment.timestamp():
        raise SsoError("The provider's ID token has already expired.", "expired_token")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise SsoError("The provider did not return an email address, so there "
                       "is nothing to match an account on.", "no_email")
    if claims.get("email_verified") is False:
        # An unverified address is an address somebody claimed. Matching an
        # account on it would let anyone who can add an unverified address at
        # the customer's IdP sign in as somebody else.
        raise SsoError("That address is not verified at your identity provider.",
                       "email_unverified")

    user_id, created = _provision(session, provider=provider, email=email,
                                  claims=claims)

    handoff = uuid.uuid4().hex
    session.execute(text("""
        UPDATE sso_flows SET consumed_at = now(), handoff_code = :h, user_id = :u
        WHERE state = :s
    """), {"h": handoff, "u": user_id, "s": state})
    session.flush()

    return Completed(user_id=user_id, email=email, handoff_code=handoff,
                     redirect_to=flow["redirect_to"], created=created)


def _provision(session, *, provider: dict, email: str, claims: dict) -> tuple[str, bool]:
    """Find the user, or make one. Returns (user_id, was_created)."""
    existing = session.execute(text("""
        SELECT id, status FROM users
        WHERE lower(email) = :e AND tenant_id = :t
    """), {"e": email, "t": provider["tenant_id"]}).mappings().first()

    if existing:
        if existing["status"] != "ACTIVE":
            # Their provider still recognises them; we decided otherwise, and
            # that decision is not overturned by a successful authentication.
            raise SsoError("That account has been deactivated.", "account_disabled")
        return existing["id"], False

    role = session.execute(text(
        "SELECT id, name FROM roles WHERE tenant_id = :t AND name = :n"),
        {"t": provider["tenant_id"], "n": provider["default_role"]}).mappings().first()
    if role is None:
        raise SsoError(
            f"This provider provisions new users as {provider['default_role']!r}, "
            f"and no such role exists in the tenant.", "unknown_default_role")
    if role["name"] == "owner":
        # Belt and braces: refused at configuration time too. An IdP deciding
        # who administers the tenant means anybody who can create an account
        # there can administer this one.
        raise SsoError("An identity provider may not provision owners.",
                       "owner_not_allowed")

    user_id = f"USR_{uuid.uuid4().hex[:12].upper()}"
    session.execute(text("""
        INSERT INTO users (id, tenant_id, merchant_id, email, role_id, status,
                           created_by, created_at)
        VALUES (:i, :t, :m, :e, :r, 'ACTIVE', 'sso', now())
    """), {"i": user_id, "t": provider["tenant_id"],
           "m": provider["default_merchant_id"], "e": email, "r": role["id"]})
    session.flush()
    log.info("sso_user_provisioned", extra={"sso": {
        "user_id": user_id, "tenant_id": provider["tenant_id"],
        "role": role["name"]}})
    return user_id, True


# --------------------------------------------------------------------------
# The handoff
# --------------------------------------------------------------------------
def redeem(session, handoff_code: str, *, now: datetime | None = None) -> str:
    """Exchange a handoff code for the user it belongs to. Single use."""
    moment = now or datetime.now(UTC)
    flow = session.execute(text("""
        SELECT state, user_id, consumed_at FROM sso_flows
        WHERE handoff_code = :h FOR UPDATE
    """), {"h": handoff_code}).mappings().first()
    if flow is None or not flow["user_id"]:
        raise SsoError("That sign-in code is not valid.", "unknown_code")

    consumed = flow["consumed_at"]
    if consumed.tzinfo is None:
        consumed = consumed.replace(tzinfo=UTC)
    if moment - consumed > timedelta(seconds=HANDOFF_TTL_SECONDS):
        raise SsoError("That sign-in code has expired. Sign in again.", "expired")

    # Cleared, not marked: a code that cannot be found cannot be replayed, and
    # the row's remaining fields are still the audit of the sign-in.
    session.execute(text(
        "UPDATE sso_flows SET handoff_code = NULL WHERE state = :s"),
        {"s": flow["state"]})
    session.flush()
    return flow["user_id"]


def prune_flows(session, *, now: datetime | None = None) -> int:
    """Drop sign-in attempts nobody completed."""
    result = session.execute(text(
        "DELETE FROM sso_flows WHERE expires_at < :t AND consumed_at IS NULL"),
        {"t": now or datetime.now(UTC)})
    session.flush()
    return result.rowcount or 0
