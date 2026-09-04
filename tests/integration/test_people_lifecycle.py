"""Joiners, movers and leavers — ADR-0048.

There was no path from "a customer signs a contract" to "their team can log in"
that did not involve an engineer with database access. Users existed because the
seeder inserted four; a leaver was offboarded with an UPDATE; a promotion was
somebody editing a JSON column.

The tests that matter are the refusals. Creating a user is easy to get right;
what is hard is the Friday-afternoon mistakes that are permanent -- offboarding
the last owner, deleting a role people hold, granting a permission that does not
exist -- and the one that would make all of this theatre, which is an offboarded
account whose token still works.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import authz, lifecycle
from app.api import security as sec


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def _as(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


OWNER = _as("USR_A_OWNER")
ANALYST = _as("USR_A_ANALYST")


# --------------------------------------------------------------------------
# The one that would make the rest theatre
# --------------------------------------------------------------------------
def test_an_offboarded_account_stops_working_immediately(client, db):
    """The bearer token never expires and cannot be revoked (ADR-0025), so the
    row is the only thing that can stop it. If `resolve` did not filter on
    status, "offboarding" would be a database change with no effect at all."""
    created = client.post("/users", headers=OWNER,
                          json={"email": "leaver@kettle.example", "role": "analyst"})
    assert created.status_code == 201
    token = {"Authorization": f"Bearer {created.json()['token']}"}

    assert client.get("/me", headers=token).status_code == 200

    user_id = created.json()["user_id"]
    assert client.patch(f"/users/{user_id}", headers=OWNER,
                        json={"status": "DISABLED"}).status_code == 200

    assert client.get("/me", headers=token).status_code == 401


def test_re_enabling_restores_the_account_but_not_the_old_token(client):
    """Since ADR-0049 offboarding revokes every token the person held, so
    re-enabling gives them a working account and not a working credential.

    That is the right way round. A token that was live when somebody was
    walked out of the building should not come back if they are re-hired six
    months later -- and by then it has expired anyway.
    """
    from app import auth

    created = client.post("/users", headers=OWNER,
                          json={"email": "returner@kettle.example", "role": "analyst"})
    old = {"Authorization": f"Bearer {created.json()['token']}"}
    user_id = created.json()["user_id"]

    client.patch(f"/users/{user_id}", headers=OWNER, json={"status": "DISABLED"})
    assert client.get("/me", headers=old).status_code == 401

    client.patch(f"/users/{user_id}", headers=OWNER, json={"status": "ACTIVE"})
    assert client.get("/me", headers=old).status_code == 401, (
        "a revoked token must not come back with the account")
    fresh = {"Authorization": f"Bearer {auth.mint(user_id)}"}
    assert client.get("/me", headers=fresh).status_code == 200


def test_a_disabled_user_is_not_routed_notifications(client, db):
    """A disabled account holding `action:refund` is not somebody who can
    approve one, and a notification sent to them is one nobody reads."""
    before = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", required=["action:refund"])}
    assert "USR_A_APPROVER" in before

    db.execute(text("UPDATE users SET status = 'DISABLED' WHERE id = 'USR_A_APPROVER'"))
    db.flush()

    after = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", required=["action:refund"])}
    assert "USR_A_APPROVER" not in after


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------
def test_the_last_owner_cannot_be_deactivated(client):
    r = client.patch("/users/USR_A_OWNER", headers=OWNER, json={"status": "DISABLED"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_owner"


def test_the_last_owner_cannot_be_demoted_either(client):
    """The same mistake wearing a different verb. Refusing one and allowing the
    other would be a door with a lock on one side."""
    r = client.patch("/users/USR_A_OWNER", headers=OWNER, json={"role": "analyst"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_owner"


def test_an_owner_can_be_deactivated_once_there_is_another(client):
    second = client.post("/users", headers=OWNER,
                         json={"email": "owner2@kettle.example", "role": "owner"})
    assert second.status_code == 201
    r = client.patch("/users/USR_A_OWNER", headers=OWNER, json={"status": "DISABLED"})
    assert r.status_code == 200, "the guard is about the LAST owner, not about owners"


def test_a_role_somebody_holds_cannot_be_deleted(db, owner):
    with pytest.raises(lifecycle.LifecycleError) as exc:
        lifecycle.delete_role(db, actor=owner, role_name="analyst")
    assert exc.value.code == "role_in_use"


def test_a_permission_that_does_not_exist_is_refused(client):
    r = client.post("/roles", headers=OWNER,
                    json={"name": "typo", "permissions": ["action:refunds"]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_permission"


def test_a_duplicate_email_is_refused_rather_than_shadowed(client):
    client.post("/users", headers=OWNER,
                json={"email": "dupe@kettle.example", "role": "analyst"})
    r = client.post("/users", headers=OWNER,
                    json={"email": "dupe@kettle.example", "role": "analyst"})
    assert r.status_code == 409
    assert "Re-enable" in r.json()["detail"]["error"]


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", [
    ("post", "/users", {"email": "x@kettle.example", "role": "analyst"}),
    ("patch", "/users/USR_A_OWNER", {"role": "analyst"}),
    ("post", "/roles", {"name": "r", "permissions": []}),
    ("put", "/roles/analyst/permissions", {"permissions": []}),
    ("get", "/users", None),
    ("get", "/access-review", None),
])
def test_administering_people_requires_the_owner_role(client, method, path, body):
    r = getattr(client, method)(path, headers=ANALYST,
                                **({"json": body} if body is not None else {}))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "role_required"


def test_a_user_from_another_merchant_is_not_administrable(client, db):
    """404, not 403: distinguishing them would tell a caller whether an id
    exists in somebody else's tenant."""
    r = client.patch("/users/USR_B_OWNER", headers=OWNER, json={"role": "analyst"})
    assert r.status_code == 404


def test_creating_a_user_puts_them_in_the_callers_merchant(client, db):
    created = client.post("/users", headers=OWNER,
                          json={"email": "scoped@kettle.example", "role": "analyst"})
    row = db.execute(text("SELECT tenant_id, merchant_id FROM users WHERE id = :u"),
                     {"u": created.json()["user_id"]}).mappings().one()
    assert row["tenant_id"] == "TEN_KETTLE"
    assert row["merchant_id"] == "MERCH_A"


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
def test_changing_a_role_changes_it_for_everybody_holding_it(client, db):
    """The entire reason roles are rows. Editing a JSON column changed one
    person; this changes the set."""
    a = client.post("/users", headers=OWNER,
                    json={"email": "one@kettle.example", "role": "analyst"}).json()
    b = client.post("/users", headers=OWNER,
                    json={"email": "two@kettle.example", "role": "analyst"}).json()

    r = client.put("/roles/analyst/permissions", headers=OWNER,
                   json={"permissions": ["read:metrics", "read:orders", "action:recover"]})
    assert r.status_code == 200
    assert r.json()["granted"] == ["action:recover"]

    for user in (a, b):
        assert "action:recover" in authz.resolve(db, user["user_id"]).permissions


def test_the_change_reports_what_was_granted_and_revoked(client):
    r = client.put("/roles/analyst/permissions", headers=OWNER,
                   json={"permissions": ["read:metrics"]})
    assert r.json()["revoked"] == ["read:orders"]
    assert r.json()["granted"] == []


def test_the_role_list_says_how_many_people_hold_each(client):
    r = client.get("/roles", headers=OWNER)
    by_name = {x["name"]: x for x in r.json()["roles"]}
    assert by_name["owner"]["held_by"] >= 1
    assert by_name["analyst"]["held_by"] >= 1
    # The catalogue rides along so a client building a picker need not guess.
    assert {p["name"] for p in r.json()["catalogue"]} >= {"action:refund", "read:orders"}


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
@pytest.mark.parametrize("event", ["user_created", "user_role_changed",
                                   "user_status_changed", "role_permissions_changed"])
def test_every_lifecycle_change_is_audited(client, db, event):
    created = client.post("/users", headers=OWNER,
                          json={"email": f"audit-{event}@kettle.example", "role": "analyst"})
    user_id = created.json()["user_id"]
    client.patch(f"/users/{user_id}", headers=OWNER, json={"role": "approver"})
    client.patch(f"/users/{user_id}", headers=OWNER, json={"status": "DISABLED"})
    client.put("/roles/analyst/permissions", headers=OWNER,
               json={"permissions": ["read:metrics"]})

    n = db.execute(text("SELECT count(*) FROM audit_logs WHERE event_type = :e"),
                   {"e": event}).scalar()
    assert n >= 1, f"{event} left no audit record"


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
def test_provisioning_creates_a_working_tenant(db):
    owner = lifecycle.provision_tenant(
        db, tenant_id="TEN_TEST", tenant_name="Test Group",
        merchant_id="MERCH_TEST", merchant_name="Test Retail",
        owner_email="first@test.example")

    resolved = authz.resolve(db, owner.user_id)
    assert resolved.role == "owner"
    assert "action:refund" in resolved.permissions
    assert resolved.tenant_id == "TEN_TEST"


def test_provisioning_is_idempotent(db):
    first = lifecycle.provision_tenant(
        db, tenant_id="TEN_IDEM", tenant_name="Idem", merchant_id="MERCH_IDEM",
        merchant_name="Idem Retail", owner_email="a@idem.example")
    second = lifecycle.provision_tenant(
        db, tenant_id="TEN_IDEM", tenant_name="Idem", merchant_id="MERCH_IDEM",
        merchant_name="Idem Retail", owner_email="a@idem.example")
    assert first.user_id == second.user_id, (
        "a re-run after a partial failure must complete, not duplicate")


def test_a_new_tenants_roles_do_not_touch_another_tenants(db):
    lifecycle.provision_tenant(
        db, tenant_id="TEN_SEP", tenant_name="Sep", merchant_id="MERCH_SEP",
        merchant_name="Sep Retail", owner_email="a@sep.example")
    rows = db.execute(text("""
        SELECT tenant_id, count(*) FROM roles
        WHERE tenant_id IN ('TEN_SEP', 'TEN_KETTLE') GROUP BY tenant_id
    """)).all()
    assert dict(rows) == {"TEN_SEP": 3, "TEN_KETTLE": 3}
