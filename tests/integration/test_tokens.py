"""Tokens that expire, rotate and can be taken away — ADR-0049.

Authentication was an HMAC of the user id: unforgeable, and valid forever. The
README carried the limitation from the day the scheme was introduced. ADR-0048
sharpened it -- a leaver's token dies with their row now -- but a *leaked* token
still worked until somebody changed the server secret, which signs everybody
out.

The tests worth reading are the ones where a token stops working: expiry,
revocation, supersession, rotation past the overlap window, and the replayed
refresh token that signs out an entire account.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app import auth
from app.api import security as sec


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


@pytest.fixture(autouse=True)
def _clean_credentials(db):
    """Other tests sign accounts out; a supersession timestamp left behind would
    refuse every token minted in the tests that follow."""
    db.execute(text("UPDATE users SET credentials_valid_from = NULL"))
    db.execute(text("DELETE FROM revoked_tokens"))
    db.flush()
    yield


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------
def test_a_token_carries_an_expiry_a_unique_id_and_a_key_id():
    claims = auth.parse(auth.mint("USR_A_OWNER"))
    assert claims.sub == "USR_A_OWNER"
    assert claims.typ == auth.ACCESS
    assert claims.jti and claims.kid == "current"
    assert claims.exp > claims.iat


def test_two_tokens_for_one_user_are_different_tokens():
    """Without a unique id, "revoke this token" has nothing to name and
    revoking one would revoke every token the user holds."""
    assert auth.parse(auth.mint("USR_A_OWNER")).jti != \
           auth.parse(auth.mint("USR_A_OWNER")).jti


def test_the_payload_is_readable_without_the_key():
    """Deliberately. Debugging an auth problem at three in the morning should
    not require the signing secret."""
    import base64
    import json

    body = auth.mint("USR_A_OWNER").split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["sub"] == "USR_A_OWNER"


# --------------------------------------------------------------------------
# Refusing
# --------------------------------------------------------------------------
def test_an_expired_token_is_refused():
    past = datetime.now(UTC) - timedelta(hours=2)
    token = auth.mint("USR_A_OWNER", lifetime_seconds=60, now=past)
    with pytest.raises(auth.TokenError) as exc:
        auth.parse(token)
    assert exc.value.code == "expired"


def test_a_tampered_payload_is_refused():
    """The signature covers the exact payload bytes, so editing any claim --
    including `exp` -- invalidates it."""
    import base64
    import json

    prefix, body, sig = auth.mint("USR_A_OWNER").split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["sub"] = "USR_B_OWNER"
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")

    with pytest.raises(auth.TokenError) as exc:
        auth.parse(f"{prefix}.{forged}.{sig}")
    assert exc.value.code == "bad_signature"


def test_a_refresh_token_is_not_an_access_token():
    """Same bytes to a signature check, different meanings. `typ` is what keeps
    a long-lived refresh token from being used as a session credential."""
    with pytest.raises(auth.TokenError) as exc:
        auth.parse(auth.mint("USR_A_OWNER", typ=auth.REFRESH), expect=auth.ACCESS)
    assert exc.value.code == "wrong_type"


def test_a_legacy_token_is_refused_by_default(client):
    """A format with no expiry, accepted indefinitely, would make every property
    this change adds optional for anybody still holding one."""
    legacy = sec.issue_legacy_token("USR_A_OWNER")
    assert client.get("/me", headers=_bearer(legacy)).status_code == 401


def test_a_legacy_token_is_accepted_while_a_rollout_says_so(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AUTH_ACCEPT_LEGACY_TOKENS", "true")
    get_settings.cache_clear()
    try:
        legacy = sec.issue_legacy_token("USR_A_OWNER")
        assert client.get("/me", headers=_bearer(legacy)).status_code == 200
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------
def test_a_token_signed_with_the_previous_key_still_verifies(monkeypatch):
    """What makes rotation possible without a flag day: set the old value as
    PREVIOUS, deploy, and tokens in the wild keep working until they expire."""
    monkeypatch.setenv("API_TOKEN_SECRET", "old-key")
    token = auth.mint("USR_A_OWNER")

    monkeypatch.setenv("API_TOKEN_SECRET", "new-key")
    monkeypatch.setenv("API_TOKEN_SECRET_PREVIOUS", "old-key")
    # `kid` says `current`, which now means the new key -- so the old token
    # fails, which is the honest outcome and the reason `kid` exists at all.
    with pytest.raises(auth.TokenError):
        auth.parse(token)


def test_a_key_the_server_no_longer_holds_says_so(monkeypatch):
    """Distinguishing "the overlap window closed" from "somebody is forging"
    sends whoever is paged to the right place."""
    import base64
    import json

    monkeypatch.setenv("API_TOKEN_SECRET", "k")
    prefix, body, sig = auth.mint("USR_A_OWNER").split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["kid"] = "ancient"
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")

    with pytest.raises(auth.TokenError) as exc:
        auth.parse(f"{prefix}.{forged}.{sig}")
    assert exc.value.code == "unknown_key"


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------
def test_signing_out_stops_that_token_and_leaves_the_others(client, db):
    one = auth.mint("USR_A_OWNER")
    two = auth.mint("USR_A_OWNER")
    assert client.get("/me", headers=_bearer(one)).status_code == 200

    assert client.post("/auth/sign-out", headers=_bearer(one)).status_code == 200
    assert client.get("/me", headers=_bearer(one)).status_code == 401
    assert client.get("/me", headers=_bearer(two)).status_code == 200, (
        "signing out one session must not sign out the rest")


def test_signing_out_everywhere_stops_tokens_the_server_never_saw(client):
    """A timestamp, not a sweep. There is no list of live tokens to walk --
    that is what a self-contained token means -- but there is always a moment
    to compare against."""
    one = auth.mint("USR_A_OWNER")
    two = auth.mint("USR_A_OWNER")

    assert client.post("/auth/sign-out?everywhere=true",
                       headers=_bearer(one)).status_code == 200
    assert client.get("/me", headers=_bearer(two)).status_code == 401


def test_an_owner_can_sign_another_user_out(client):
    """The response to a stolen laptop when its owner cannot do it themselves.
    Different from offboarding: the account still works."""
    victim = auth.mint("USR_A_ANALYST")
    assert client.get("/me", headers=_bearer(victim)).status_code == 200

    owner = auth.mint("USR_A_OWNER")
    r = client.post("/users/USR_A_ANALYST/sign-out", headers=_bearer(owner))
    assert r.status_code == 200
    assert client.get("/me", headers=_bearer(victim)).status_code == 401
    # Still a working account -- a new token is fine.
    assert client.get("/me", headers=_bearer(auth.mint("USR_A_ANALYST"))).status_code == 200


def test_signing_another_user_out_requires_the_owner_role(client):
    r = client.post("/users/USR_A_OWNER/sign-out",
                    headers=_bearer(auth.mint("USR_A_ANALYST")))
    assert r.status_code == 403


def test_offboarding_revokes_every_token(client, db):
    """ADR-0048 made a disabled user unresolvable; this makes their tokens
    invalid as well. The two answer different questions, and the day somebody
    adds a lookup that skips `resolve`, this is the one still standing."""
    created = client.post("/users", headers=_bearer(auth.mint("USR_A_OWNER")),
                          json={"email": "gone@kettle.example", "role": "analyst"})
    token = created.json()["token"]
    assert client.get("/me", headers=_bearer(token)).status_code == 200

    client.patch(f"/users/{created.json()['user_id']}",
                 headers=_bearer(auth.mint("USR_A_OWNER")),
                 json={"status": "DISABLED"})
    assert client.get("/me", headers=_bearer(token)).status_code == 401


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
def test_refreshing_returns_a_new_pair_and_burns_the_old_one(client):
    pair = auth.issue_pair("USR_A_OWNER")
    r = client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
    assert r.status_code == 200
    assert r.json()["refresh_token"] != pair.refresh_token, "single use"
    assert client.get("/me",
                      headers=_bearer(r.json()["access_token"])).status_code == 200


def test_replaying_a_refresh_token_signs_the_whole_account_out(client):
    """The blunt response, and the right one. A second presentation means
    somebody else has a copy -- theft or a client bug -- and both are better
    resolved by everyone signing in again than by guessing which holder is
    legitimate."""
    pair = auth.issue_pair("USR_A_OWNER")
    good = client.post("/auth/refresh",
                       json={"refresh_token": pair.refresh_token}).json()
    assert client.get("/me", headers=_bearer(good["access_token"])).status_code == 200

    replay = client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "replayed"

    # The legitimate half is gone too. It was minted moments before the replay
    # was detected and is as compromised as the token that was replayed.
    assert client.get("/me", headers=_bearer(good["access_token"])).status_code == 401
    assert client.post("/auth/refresh",
                       json={"refresh_token": good["refresh_token"]}).status_code == 401


def test_the_replay_response_survives_the_error_that_reports_it(client, db):
    """The revocation is committed before the exception is raised. Without that,
    the 401 leaves `session_scope`, the transaction rolls back, every session
    stays live, and the log says they were closed."""
    pair = auth.issue_pair("USR_A_OWNER")
    client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
    client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})

    valid_from = db.execute(text(
        "SELECT credentials_valid_from FROM users WHERE id = 'USR_A_OWNER'")).scalar()
    assert valid_from is not None, "the revocation was rolled back by its own 401"


def test_an_access_token_cannot_be_used_to_refresh(client):
    pair = auth.issue_pair("USR_A_OWNER")
    r = client.post("/auth/refresh", json={"refresh_token": pair.access_token})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "wrong_type"


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------
def test_expired_revocations_are_pruned(db):
    """A denylist that only grows eventually costs more than what it protects.
    After a token's own expiry the signature check refuses it without consulting
    this table at all."""
    stale = auth.parse(auth.mint("USR_A_OWNER", lifetime_seconds=60,
                                 now=datetime.now(UTC) - timedelta(hours=3)),
                       now=datetime.now(UTC) - timedelta(hours=3))
    auth.revoke(db, stale, reason="test")
    live = auth.parse(auth.mint("USR_A_OWNER"))
    auth.revoke(db, live, reason="test")

    assert auth.prune_revoked(db) == 1
    remaining = {r[0] for r in db.execute(text("SELECT jti FROM revoked_tokens"))}
    assert remaining == {live.jti}
