"""API authentication and rate limiting — closes two threat-model residual risks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import security as sec
from app.api.main import app


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


# ---------------------------------------------------------------- tokens
def test_signature_verifies():
    t = sec.issue_token("USR_A_OWNER")
    assert sec.verify_token(t) == "USR_A_OWNER"


def test_forged_signature_is_rejected():
    assert sec.verify_token("USR_A_OWNER." + "f" * 64) is None


def test_cannot_swap_the_subject_and_keep_the_signature():
    """The signature covers the user id, so lifting it onto another identity fails."""
    good = sec.issue_token("USR_A_OWNER")
    sig = good.split(".", 1)[1]
    assert sec.verify_token(f"USR_B_OWNER.{sig}") is None


def test_malformed_tokens_are_rejected():
    for bad in ("", "nodot", ".", "USR_A_OWNER.", ".sig", "Bearer x"):
        assert sec.verify_token(bad) is None


def test_secret_change_invalidates_existing_tokens(monkeypatch):
    t = sec.issue_token("USR_A_OWNER")
    monkeypatch.setenv("API_TOKEN_SECRET", "a-different-secret")
    assert sec.verify_token(t) is None


# ---------------------------------------------------------------- endpoints
def test_request_without_token_is_401(client):
    r = client.get("/tasks/ANY")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_request_with_forged_token_is_401(client):
    r = client.get("/tasks/ANY", headers={"Authorization": "Bearer USR_A_OWNER." + "0" * 64})
    assert r.status_code == 401


def test_valid_token_is_accepted(client, db):
    # An authenticated route: 404 for the unknown task, but only after auth passed.
    assert client.get("/tasks/NOPE", headers=token("USR_A_OWNER")).status_code == 404


def test_token_for_deleted_principal_is_rejected(client, db):
    """A token can outlive its subject; the database is still the authority."""
    t = sec.issue_token("USR_DOES_NOT_EXIST")
    # /health is deliberately public, so this must target an authenticated route.
    r = client.get("/tasks/NOPE", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 401


def test_permissions_come_from_the_database_not_the_token(client, db):
    """The token carries only an identity. Elevating requires changing the
    database, not minting a different token."""
    r = client.post("/tasks", json={"request": "Refund the duplicate payment."},
                    headers=token("USR_A_ANALYST"))
    assert r.status_code == 200
    body = r.json()
    assert body["approvals"] == []          # analyst never reaches an approval
    assert body["actions"] == []


def test_cross_merchant_task_is_not_visible(client, db):
    created = client.post("/tasks", json={"request": "Why did revenue drop this week?"},
                          headers=token("USR_A_OWNER"))
    task_id = created.json()["id"]
    assert client.get(f"/tasks/{task_id}", headers=token("USR_A_OWNER")).status_code == 200
    # Existence must not leak across merchants: 404, not 403.
    assert client.get(f"/tasks/{task_id}", headers=token("USR_B_OWNER")).status_code == 404


# ---------------------------------------------------------------- rate limiting
def test_action_class_is_rate_limited(client, db):
    hdr = token("USR_A_OWNER")
    codes = [client.post("/tasks/NOPE/approve", headers=hdr).status_code
             for _ in range(sec.LIMITS["action"].requests + 2)]
    assert 429 in codes
    assert codes.index(429) == sec.LIMITS["action"].requests


def test_rate_limit_response_tells_the_caller_when_to_retry(client, db):
    hdr = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        r = client.post("/tasks/NOPE/approve", headers=hdr)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert r.json()["detail"]["error"] == "rate_limit_exceeded"


def test_limit_classes_have_independent_budgets(client, db):
    hdr = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        client.post("/tasks/NOPE/approve", headers=hdr)
    # Reads must still work after the action budget is spent.
    assert client.get("/tasks/NOPE", headers=hdr).status_code == 404


def test_limits_are_per_principal(client, db):
    a = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        client.post("/tasks/NOPE/approve", headers=a)
    assert client.post("/tasks/NOPE/approve", headers=a).status_code == 429
    # One principal exhausting its budget must not deny another.
    assert client.post("/tasks/NOPE/approve",
                       headers=token("USR_B_OWNER")).status_code != 429


def test_unauthenticated_requests_cannot_consume_a_principal_budget(client, db):
    """Rate limiting runs AFTER authentication, so an anonymous flood cannot
    exhaust a real user's allowance."""
    for _ in range(sec.LIMITS["action"].requests + 5):
        client.post("/tasks/NOPE/approve")
    assert client.post("/tasks/NOPE/approve",
                       headers=token("USR_A_OWNER")).status_code != 429
