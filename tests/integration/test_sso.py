"""Signing in through the customer's identity provider — ADR-0050.

Every enterprise security review asks for this before it asks anything else.

The tests that matter are the refusals. A working sign-in is one path; what
decides whether this is safe is what happens to a callback with somebody else's
`state`, an ID token minted for a different client, a replayed authorization
code, and a redirect that points off-site.

The provider is a stub. Standing up a real OIDC server would test that server;
what needs testing here is what this code does with the answers, including the
answers a hostile provider or a man in the middle would give.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app import sso
from app.api import security as sec

ISSUER = "https://idp.kettle.example"
CLIENT_ID = "merchantops-kettle"
REDIRECT_URI = "http://testserver/auth/sso/callback"


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


@pytest.fixture(autouse=True)
def _stub_provider(db, monkeypatch):
    """A configured provider, and discovery that answers without a network."""
    sso.forget_discovery()
    db.execute(text("""
        INSERT INTO identity_providers (id, tenant_id, issuer, client_id,
            client_secret, email_domains, default_role, default_merchant_id,
            enabled, created_at, updated_at)
        VALUES ('IDP_TEST', 'TEN_KETTLE', :iss, :cid, 'shh',
                '["kettle.example"]'::json, 'analyst', 'MERCH_A', true,
                now(), now())
        ON CONFLICT (tenant_id) DO UPDATE SET issuer = EXCLUDED.issuer
    """), {"iss": ISSUER, "cid": CLIENT_ID})
    db.flush()

    monkeypatch.setattr(sso, "discover", lambda issuer, **kw: {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
    })
    yield
    sso.forget_discovery()


def _id_token(*, nonce: str, email="new@kettle.example", iss=ISSUER,
              aud=CLIENT_ID, exp_delta=300, email_verified=True) -> str:
    """An unsigned ID token. The signature is not checked in this flow (OIDC
    Core §3.1.3.7) because the token arrives over authenticated TLS from the
    token endpoint -- so a stub that omits it exercises the real path."""
    claims = {"iss": iss, "aud": aud, "nonce": nonce, "email": email,
              "email_verified": email_verified,
              "exp": (datetime.now(UTC) + timedelta(seconds=exp_delta)).timestamp()}
    body = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


class _TokenEndpoint:
    """Stands in for `httpx.post` to the provider's token endpoint."""

    def __init__(self, id_token_for, status=200):
        self.id_token_for = id_token_for
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs.get("data", {}))

        class _Response:
            status_code = self.status
            text = "stub"

            def json(_self):
                return {"id_token": self.id_token_for(self.calls[-1])}

        return _Response()


def _start(db, email="new@kettle.example"):
    return sso.start(db, email=email, redirect_uri=REDIRECT_URI)


def _flow(db, state: str) -> dict:
    return dict(db.execute(text("SELECT * FROM sso_flows WHERE state = :s"),
                           {"s": state}).mappings().one())


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------
def test_a_sign_in_routes_to_the_tenant_that_owns_the_domain(db):
    started = _start(db)
    assert started.authorization_url.startswith(f"{ISSUER}/authorize?")
    assert _flow(db, started.state)["tenant_id"] == "TEN_KETTLE"


def test_the_authorization_request_carries_state_nonce_and_pkce(db):
    started = _start(db)
    url = started.authorization_url
    for parameter in ("state=", "nonce=", "code_challenge=",
                      "code_challenge_method=S256", "response_type=code"):
        assert parameter in url, f"{parameter} missing from {url}"
    # S256, never `plain` -- `plain` sends the verifier in the authorization
    # request, which is the thing PKCE exists to avoid.
    assert "code_challenge_method=plain" not in url


def test_an_unknown_domain_is_refused_without_saying_why(db):
    with pytest.raises(sso.SsoError) as exc:
        _start(db, email="someone@notacustomer.example")
    assert exc.value.code == "no_provider"
    # Same answer as "configured but disabled": distinguishing them tells an
    # unauthenticated caller which companies are customers.
    assert "not a customer" not in str(exc.value).lower()


@pytest.mark.parametrize("target,expected", [
    ("/incidents", "/incidents"),
    ("https://evil.example/steal", "/"),
    ("//evil.example/steal", "/"),
    ("javascript:alert(1)", "/"),
    (None, "/"),
])
def test_an_off_site_redirect_is_discarded(db, target, expected):
    """An open redirect on a login endpoint is how a phishing page borrows
    somebody else's domain: the victim sees a legitimate host, signs in, and is
    handed to the attacker."""
    started = sso.start(db, email="new@kettle.example",
                        redirect_uri=REDIRECT_URI, redirect_to=target)
    assert _flow(db, started.state)["redirect_to"] == expected


# --------------------------------------------------------------------------
# Coming back
# --------------------------------------------------------------------------
def test_a_successful_sign_in_provisions_and_hands_off(db, monkeypatch):
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: _id_token(nonce=nonce)))

    done = sso.complete(db, state=started.state, code="abc",
                        redirect_uri=REDIRECT_URI)
    assert done.created is True
    assert done.email == "new@kettle.example"
    assert done.handoff_code

    from app import authz
    person = authz.resolve(db, done.user_id)
    assert person.role == "analyst", "the provider's default_role, not owner"
    assert person.tenant_id == "TEN_KETTLE"


def test_the_code_verifier_is_sent_to_the_token_endpoint(db, monkeypatch):
    """PKCE only works if the verifier actually goes back. An intercepted
    authorization code is useless without it."""
    started = _start(db)
    flow = _flow(db, started.state)
    endpoint = _TokenEndpoint(lambda data: _id_token(nonce=flow["nonce"]))
    monkeypatch.setattr(sso.httpx, "post", endpoint)

    sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert endpoint.calls[0]["code_verifier"] == flow["code_verifier"]


def test_an_existing_user_is_matched_not_duplicated(db, monkeypatch):
    started = _start(db, email="owner@kettle.example")
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post", _TokenEndpoint(
        lambda data: _id_token(nonce=nonce, email="owner@kettle.example")))

    done = sso.complete(db, state=started.state, code="abc",
                        redirect_uri=REDIRECT_URI)
    assert done.created is False
    assert done.user_id == "USR_A_OWNER"


def test_a_deactivated_user_cannot_sign_back_in(db, monkeypatch):
    """Their provider still recognises them. We decided otherwise, and a
    successful authentication is not new information about that."""
    db.execute(text("UPDATE users SET status='DISABLED' WHERE id='USR_A_ANALYST'"))
    db.flush()
    started = _start(db, email="analyst@kettle.example")
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post", _TokenEndpoint(
        lambda data: _id_token(nonce=nonce, email="analyst@kettle.example")))

    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == "account_disabled"


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------
def test_a_callback_we_did_not_start_is_refused(db):
    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=uuid.uuid4().hex, code="abc",
                     redirect_uri=REDIRECT_URI)
    assert exc.value.code == "unknown_state"


def test_a_replayed_callback_is_refused(db, monkeypatch):
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: _id_token(nonce=nonce)))
    sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)

    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == "state_reused"


def test_an_expired_flow_is_refused(db, monkeypatch):
    started = _start(db)
    db.execute(text("UPDATE sso_flows SET expires_at = :t WHERE state = :s"),
               {"t": datetime.now(UTC) - timedelta(minutes=1), "s": started.state})
    db.flush()
    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == "expired"


@pytest.mark.parametrize("kwargs,code", [
    ({"iss": "https://attacker.example"}, "issuer_mismatch"),
    ({"aud": "some-other-client"}, "audience_mismatch"),
    ({"exp_delta": -60}, "expired_token"),
    ({"email": ""}, "no_email"),
    ({"email_verified": False}, "email_unverified"),
])
def test_a_bad_id_token_is_refused(db, monkeypatch, kwargs, code):
    """Everything TLS does not establish is checked. `iss` and `aud` stop a
    token minted for somebody else being accepted here; `email_verified` stops
    anyone who can add an unverified address at the customer's IdP signing in
    as somebody else."""
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post", _TokenEndpoint(
        lambda data: _id_token(nonce=nonce, **kwargs)))

    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == code


def test_an_id_token_answering_a_different_sign_in_is_refused(db, monkeypatch):
    """The nonce binds the token to THIS attempt. Without it, a token obtained
    in one sign-in could be replayed into another."""
    started = _start(db)
    monkeypatch.setattr(sso.httpx, "post", _TokenEndpoint(
        lambda data: _id_token(nonce="a-different-attempt")))

    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == "nonce_mismatch"


def test_a_refused_authorization_code_is_reported_not_swallowed(db, monkeypatch):
    started = _start(db)
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: "", status=400))
    with pytest.raises(sso.SsoError) as exc:
        sso.complete(db, state=started.state, code="abc", redirect_uri=REDIRECT_URI)
    assert exc.value.code == "token_exchange_failed"


# --------------------------------------------------------------------------
# The handoff
# --------------------------------------------------------------------------
def test_the_handoff_code_is_single_use(db, monkeypatch):
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: _id_token(nonce=nonce)))
    done = sso.complete(db, state=started.state, code="abc",
                        redirect_uri=REDIRECT_URI)

    assert sso.redeem(db, done.handoff_code) == done.user_id
    with pytest.raises(sso.SsoError) as exc:
        sso.redeem(db, done.handoff_code)
    assert exc.value.code == "unknown_code"


def test_an_expired_handoff_code_is_refused(db, monkeypatch):
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: _id_token(nonce=nonce)))
    done = sso.complete(db, state=started.state, code="abc",
                        redirect_uri=REDIRECT_URI)

    later = datetime.now(UTC) + timedelta(seconds=sso.HANDOFF_TTL_SECONDS + 30)
    with pytest.raises(sso.SsoError) as exc:
        sso.redeem(db, done.handoff_code, now=later)
    assert exc.value.code == "expired"


def test_no_credential_ever_appears_in_a_url(db, monkeypatch, client):
    """The reason the handoff code exists. A token in a fragment or query lands
    in browser history, in the referrer of whatever loads next, and in every
    proxy log on the way."""
    started = _start(db)
    nonce = _flow(db, started.state)["nonce"]
    monkeypatch.setattr(sso.httpx, "post",
                        _TokenEndpoint(lambda data: _id_token(nonce=nonce)))
    db.commit()

    r = client.get(f"/auth/sso/callback?state={started.state}&code=abc",
                   follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert "sso=" in location
    assert "mo1." not in location, "a token reached the redirect URL"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def test_configuring_sso_requires_the_owner_role(client):
    r = client.put("/sso", headers={"Authorization":
                                    f"Bearer {sec.issue_token('USR_A_ANALYST')}"},
                   json={"issuer": ISSUER, "client_id": "x", "client_secret": "y",
                         "email_domains": ["kettle.example"]})
    assert r.status_code == 403


def test_a_provider_may_not_provision_owners(client):
    r = client.put("/sso", headers={"Authorization":
                                    f"Bearer {sec.issue_token('USR_A_OWNER')}"},
                   json={"issuer": ISSUER, "client_id": "x", "client_secret": "y",
                         "email_domains": ["kettle.example"],
                         "default_role": "owner"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "owner_not_allowed"


def test_the_client_secret_is_never_returned(client, db):
    db.commit()
    r = client.get("/sso", headers={"Authorization":
                                   f"Bearer {sec.issue_token('USR_A_OWNER')}"})
    assert r.status_code == 200
    assert "shh" not in r.text
    assert "client_secret" not in r.json()
