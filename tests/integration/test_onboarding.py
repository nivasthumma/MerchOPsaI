"""Bringing a customer onto the platform — §45.

Two halves with different authorities, and the split is the point:

    POST /merchants          an owner adds a merchant to THEIR OWN tenant
    scripts/onboard_tenant   an operator stands up a tenant that has no owner

The second cannot be an endpoint. It mints the first owner of a tenant nobody
administers yet, so no principal inside the system could authorise it — and the
two ways to make it authorisable are both worse than a script: a platform
superuser sitting above every tenant boundary, or letting any owner mint
tenants they then control, which is escalation rather than onboarding.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.api import security as sec


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def _as(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


OWNER = "USR_A_OWNER"
ANALYST = "USR_A_ANALYST"
OWNER_B = "USR_B_OWNER"


# --------------------------------------------------------------------------
# Adding a merchant to a tenant that already has an owner
# --------------------------------------------------------------------------
def test_an_owner_can_add_a_merchant_to_their_tenant(client):
    r = client.post("/merchants", headers=_as(OWNER),
                    json={"name": "Kettle Espresso"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant_id"] == "TEN_KETTLE"
    assert body["name"] == "Kettle Espresso"
    assert body["merchant_id"].startswith("MERCH_")


def test_a_body_naming_a_tenant_is_refused_outright(client):
    """The one thing this endpoint must not honour.

    A merchant id is caller-supplied; the tenant is not. `Contract` forbids
    extra keys, so an attempt to name one is a 422 rather than a field quietly
    ignored — which is the stronger answer: a caller who thinks they set the
    tenant and got a 201 has been misled about what happened.
    """
    r = client.post("/merchants", headers=_as(OWNER),
                    json={"name": "Smuggled", "tenant_id": "TEN_NORTHWIND"})
    assert r.status_code == 422


def test_the_tenant_comes_from_the_principal(client, db):
    """And the positive half: whatever else is in the request, the merchant
    lands in the caller's own tenant."""
    r = client.post("/merchants", headers=_as(OWNER),
                    json={"name": "Second Storefront", "merchant_id": "MERCH_SECOND"})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "TEN_KETTLE"

    landed = db.execute(text("SELECT tenant_id FROM merchants WHERE id = 'MERCH_SECOND'")
                        ).scalar()
    assert landed == "TEN_KETTLE"


def test_a_non_owner_cannot_create_a_merchant(client):
    r = client.post("/merchants", headers=_as(ANALYST), json={"name": "Nope"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "role_required"


def test_a_duplicate_id_is_refused_without_saying_whose_it_is(client):
    """409, and deliberately not an id oracle.

    Saying "that belongs to Northwind" would let an owner enumerate another
    tenant's merchant ids one guess at a time.
    """
    r = client.post("/merchants", headers=_as(OWNER),
                    json={"name": "Clash", "merchant_id": "MERCH_B"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "merchant_exists"
    assert "TEN_NORTHWIND" not in str(detail)
    assert "Northwind" not in str(detail)


def test_creating_a_merchant_is_recorded(client, db):
    client.post("/merchants", headers=_as(OWNER), json={"name": "Kettle Roastery"})
    rows = db.execute(text(
        "SELECT payload FROM audit_logs WHERE event_type = 'merchant_created'")
    ).scalars().all()
    assert rows, "a merchant appeared with nothing in the trail saying so"


def test_listing_merchants_is_scoped_to_the_callers_tenant(client):
    kettle = client.get("/merchants", headers=_as(OWNER)).json()["merchants"]
    northwind = client.get("/merchants", headers=_as(OWNER_B)).json()["merchants"]

    assert {m["merchant_id"] for m in kettle} == {"MERCH_A", "MERCH_C"}
    assert {m["merchant_id"] for m in northwind} == {"MERCH_B"}


# --------------------------------------------------------------------------
# Standing up a whole customer
# --------------------------------------------------------------------------
def test_onboarding_creates_a_working_tenant_in_one_transaction(db):
    """Tenant, merchant, roles and a first owner who can actually be resolved.

    The assertion that matters is the last one: a tenant with rows but no
    resolvable owner looks exactly like a customer and cannot be administered.
    """
    from app import authz
    from scripts.onboard_tenant import onboard

    out = onboard(db, tenant_name="Northwind Holdings",
                  merchant_name="Northwind Traders",
                  owner_email="ops@northwind.example", actor="tester")
    db.flush()

    assert db.execute(text("SELECT name FROM tenants WHERE id = :i"),
                      {"i": out["tenant_id"]}).scalar() == "Northwind Holdings"
    assert db.execute(text("SELECT tenant_id FROM merchants WHERE id = :i"),
                      {"i": out["merchant_id"]}).scalar() == out["tenant_id"]

    roles = db.execute(text("SELECT name FROM roles WHERE tenant_id = :t"),
                       {"t": out["tenant_id"]}).scalars().all()
    assert set(roles) == {"owner", "approver", "analyst"}

    resolved = authz.resolve(db, out["owner_user_id"])
    assert resolved is not None, "the first owner cannot be resolved"
    assert resolved.role == "owner"
    assert resolved.tenant_id == out["tenant_id"]


def test_the_first_owner_gets_a_token_once(db):
    from scripts.onboard_tenant import onboard

    out = onboard(db, tenant_name="Acme", merchant_name="Acme Retail",
                  owner_email="ops@acme.example", actor="tester")
    assert out["token"], "no credential for the account nobody can otherwise reach"
    # Not stored: the database keeps a digest, which is why the script prints it
    # once and says so.
    assert db.execute(text("SELECT COUNT(*) FROM users WHERE id = :i"),
                      {"i": out["owner_user_id"]}).scalar_one() == 1


def test_onboarding_writes_who_did_it(db):
    """A tenant that appeared with only a `created_at` to explain it is the gap
    the trail exists to close."""
    from scripts.onboard_tenant import onboard

    out = onboard(db, tenant_name="Globex", merchant_name="Globex Retail",
                  owner_email="ops@globex.example", actor="alice")
    db.flush()

    payload = db.execute(text(
        "SELECT payload FROM audit_logs WHERE event_type = 'tenant_onboarded' "
        "ORDER BY id DESC LIMIT 1")).scalar_one()
    assert out["tenant_id"] in str(payload)
    assert "alice" in str(payload)


def test_two_customers_with_the_same_name_do_not_collide(db):
    """An id collision on a customer's first day is a bad first day."""
    from scripts.onboard_tenant import onboard

    a = onboard(db, tenant_name="Kettle", merchant_name="Kettle",
                owner_email="a@one.example", actor="tester")
    b = onboard(db, tenant_name="Kettle", merchant_name="Kettle",
                owner_email="b@two.example", actor="tester")

    assert a["tenant_id"] != b["tenant_id"]
    assert a["merchant_id"] != b["merchant_id"]


def test_the_new_owner_can_administer_their_own_tenant_and_no_other(db):
    """What onboarding is FOR: somebody who can now add their colleagues.

    And the boundary in the same breath — the new owner is an owner of their
    tenant, not of the platform.
    """
    from app import authz, lifecycle
    from scripts.onboard_tenant import onboard

    out = onboard(db, tenant_name="Initech", merchant_name="Initech Retail",
                  owner_email="ops@initech.example", actor="tester")
    db.flush()

    owner = authz.resolve(db, out["owner_user_id"])
    colleague = lifecycle.create_user(db, actor=owner,
                                      email="analyst@initech.example",
                                      role_name="analyst")
    db.flush()

    assert authz.resolve(db, colleague.user_id).tenant_id == out["tenant_id"]
    # The seeded tenant is untouched by any of this.
    assert authz.resolve(db, "USR_A_OWNER").tenant_id == "TEN_KETTLE"
