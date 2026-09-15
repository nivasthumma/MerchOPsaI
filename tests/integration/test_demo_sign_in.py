"""Demo sign-in: the seeded accounts, one click each, only where it is safe.

A demo account anybody can use is only acceptable on a deployment with
synthetic data and a mock payment provider. These tests pin the three things
that make it so: it is off unless enabled, it is refused whenever payment
execution is not mocked, and it only ever signs in as an allowlisted account
that still exists and is active.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import security as sec
from app.api.main import app
from app.config import get_settings
from app.models import AuditLog

DEMO_USERS = {"USR_A_OWNER", "USR_A_APPROVER", "USR_A_ANALYST", "USR_B_OWNER"}


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


@pytest.fixture
def demo_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "demo_sign_in_enabled", True)
    monkeypatch.setattr(s, "razorpay_mode", "mock")
    return s


def test_demo_sign_in_is_off_unless_enabled(client):
    body = client.get("/auth/demo").json()
    assert body["enabled"] is False
    assert body["accounts"] == []
    assert "not enabled" in body["reason"]
    assert client.post("/auth/demo", json={"user_id": "USR_A_OWNER"}).status_code == 404


def test_it_lists_the_seeded_accounts_with_the_roles_the_database_gives_them(client, demo_on):
    body = client.get("/auth/demo").json()
    accounts = {a["user_id"]: a for a in body["accounts"]}
    assert body["enabled"] is True
    assert set(accounts) == DEMO_USERS
    assert accounts["USR_A_OWNER"]["role"] == "owner"
    assert accounts["USR_A_ANALYST"]["role"] == "analyst"
    assert accounts["USR_B_OWNER"]["merchant_id"] == "MERCH_B"


def test_a_demo_account_signs_in_with_a_token_pair_that_works(client, demo_on, db):
    r = client.post("/auth/demo", json={"user_id": "USR_A_ANALYST"})
    assert r.status_code == 200, r.text
    pair = r.json()

    me = client.get("/me", headers={"Authorization": f"Bearer {pair['access_token']}"})
    assert me.status_code == 200
    assert me.json()["user_id"] == "USR_A_ANALYST"
    assert me.json()["role"] == "analyst"

    # The same refresh flow a real session uses, so a demo session outlives
    # its first hour exactly as any other does.
    assert client.post("/auth/refresh",
                       json={"refresh_token": pair["refresh_token"]}).status_code == 200

    row = (db.query(AuditLog).filter(AuditLog.event_type == "demo_sign_in")
           .order_by(AuditLog.id.desc()).first())
    assert row is not None
    assert row.user_id == "USR_A_ANALYST"
    assert row.actor_type == "HUMAN" and row.actor == "USR_A_ANALYST"


def test_only_an_allowlisted_account_can_be_used(client, demo_on):
    r = client.post("/auth/demo", json={"user_id": "USR_SOMEONE_ELSE"})
    assert r.status_code == 403


def test_an_offboarded_demo_account_is_neither_listed_nor_usable(client, demo_on, db):
    db.execute(text("UPDATE users SET status = 'DISABLED' WHERE id = 'USR_A_APPROVER'"))
    db.commit()
    listed = {a["user_id"] for a in client.get("/auth/demo").json()["accounts"]}
    assert "USR_A_APPROVER" not in listed
    assert client.post("/auth/demo", json={"user_id": "USR_A_APPROVER"}).status_code == 403


def test_demo_sign_in_is_refused_whenever_payment_execution_is_not_mocked(
        client, demo_on, monkeypatch):
    monkeypatch.setattr(demo_on, "razorpay_mode", "live_test_mode")
    body = client.get("/auth/demo").json()
    assert body["enabled"] is False and body["accounts"] == []
    assert "not mocked" in body["reason"]
    assert client.post("/auth/demo", json={"user_id": "USR_A_OWNER"}).status_code == 404
