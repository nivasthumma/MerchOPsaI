"""SCIM 2.0 provisioning — ADR-0051.

ADR-0050 let a customer's employees sign in through their identity provider. It
did not let the provider tell us when one of them leaves, so an employee removed
at Okta kept working here until an owner remembered to disable them.

Deprovisioning is the operation everything else here exists to support, and it
is the first thing tested. The rest is the shape providers actually send: Okta
asks `userName eq` before it decides to create, and Entra deactivates with a
PATCH that has no `path`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import auth, scim
from app.api import security as sec

USER_SCHEMA = scim.USER_SCHEMA


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


@pytest.fixture
def token(db):
    """A provisioning credential for TEN_KETTLE."""
    _, value = scim.mint_token(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                               role="analyst", name="okta", created_by="USR_A_OWNER")
    db.commit()
    return {"Authorization": f"Bearer {value}"}


def _owner() -> dict:
    return {"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}


def _create(client, token, **kw) -> dict:
    body = {"schemas": [USER_SCHEMA], "userName": kw.pop("email", "new@kettle.example"),
            "active": True, **kw}
    r = client.post("/scim/v2/Users", headers=token, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------
# The reason this exists
# --------------------------------------------------------------------------
def test_deactivating_through_scim_stops_the_account_and_its_tokens(client, token, db):
    """The gap ADR-0050 left open. An employee removed at the IdP has to stop
    working here, and a deprovisioning that leaves a live session is
    deprovisioning in name only."""
    created = _create(client, token, email="leaver@kettle.example")
    user_id = created["id"]
    their_token = {"Authorization": f"Bearer {auth.mint(user_id)}"}
    assert client.get("/me", headers=their_token).status_code == 200

    r = client.patch(f"/scim/v2/Users/{user_id}", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "value": {"active": False}}],
    })
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert client.get("/me", headers=their_token).status_code == 401


def test_entra_sends_patch_without_a_path(client, token):
    """`{"op": "replace", "value": {"active": false}}` — no `path`. Handling
    only the pathed form would mean deprovisioning silently doing nothing for
    every Entra customer."""
    created = _create(client, token, email="entra@kettle.example")
    r = client.patch(f"/scim/v2/Users/{created['id']}", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "value": {"active": False}}],
    })
    assert r.json()["active"] is False


def test_okta_sends_patch_with_a_path(client, token):
    created = _create(client, token, email="okta@kettle.example")
    r = client.patch(f"/scim/v2/Users/{created['id']}", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    })
    assert r.json()["active"] is False


def test_delete_deactivates_and_keeps_the_row(client, token, db):
    """RFC 7644 §3.6 permits disabling instead of removing. The audit trail
    points at this row and has to outlive the employment."""
    created = _create(client, token, email="deleted@kettle.example")
    assert client.delete(f"/scim/v2/Users/{created['id']}",
                         headers=token).status_code == 204

    still_there = db.execute(text("SELECT status FROM users WHERE id = :i"),
                             {"i": created["id"]}).scalar()
    assert still_there == "DISABLED"


def test_a_patch_on_an_unmodelled_attribute_does_not_break_deprovisioning(client, token):
    """A provider syncing a phone number must not have its `active` operation
    rejected because of it."""
    created = _create(client, token, email="phone@kettle.example")
    r = client.patch(f"/scim/v2/Users/{created['id']}", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [
            {"op": "replace", "path": "phoneNumbers[type eq \"work\"].value",
             "value": "+44 20 7946 0000"},
            {"op": "replace", "path": "active", "value": False},
        ],
    })
    assert r.status_code == 200
    assert r.json()["active"] is False


# --------------------------------------------------------------------------
# Create-or-update, the way a provider decides
# --------------------------------------------------------------------------
def test_a_provider_finds_an_existing_user_by_username(client, token):
    """Okta asks this before it decides to create. Answering it wrong is how a
    person ends up with two accounts."""
    r = client.get('/scim/v2/Users?filter=userName eq "owner@kettle.example"',
                   headers=token)
    assert r.status_code == 200
    body = r.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "owner@kettle.example"


def test_a_filter_that_matches_nobody_is_an_empty_list_not_an_error(client, token):
    r = client.get('/scim/v2/Users?filter=userName eq "nobody@kettle.example"',
                   headers=token)
    assert r.status_code == 200
    assert r.json()["totalResults"] == 0
    assert r.json()["Resources"] == []


def test_creating_a_duplicate_is_409_with_the_uniqueness_type(client, token):
    """Providers switch from create to update on exactly this. A 500 here
    duplicates the person instead."""
    _create(client, token, email="dupe@kettle.example")
    r = client.post("/scim/v2/Users", headers=token, json={
        "schemas": [USER_SCHEMA], "userName": "dupe@kettle.example"})
    assert r.status_code == 409
    assert r.json()["detail"]["scimType"] == "uniqueness"


def test_an_unsupported_filter_says_so_rather_than_returning_everything(client, token):
    """Ignoring a filter it does not understand would hand a provider the whole
    directory and let it conclude every user matches."""
    r = client.get('/scim/v2/Users?filter=userName sw "a"', headers=token)
    assert r.status_code == 400
    assert r.json()["detail"]["scimType"] == "invalidFilter"


def test_the_external_id_is_kept_so_a_rename_is_not_a_new_person(client, token):
    created = _create(client, token, email="ext@kettle.example",
                      externalId="okta-00u123")
    assert created["externalId"] == "okta-00u123"
    r = client.get('/scim/v2/Users?filter=externalId eq "okta-00u123"', headers=token)
    assert r.json()["Resources"][0]["id"] == created["id"]


def test_a_user_created_by_scim_can_sign_in(client, token, db):
    created = _create(client, token, email="works@kettle.example")
    from app import authz
    person = authz.resolve(db, created["id"])
    assert person is not None
    assert person.role == "analyst", "the token's default_role"
    assert person.merchant_id == "MERCH_A"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------
def test_no_token_is_a_scim_error_not_a_merchantops_one(client):
    """A provider that gets an unfamiliar error body reports "invalid response"
    and stops, which is a support ticket rather than a diagnosis."""
    r = client.get("/scim/v2/Users")
    assert r.status_code == 401
    assert scim.ERROR_SCHEMA in r.json()["detail"]["schemas"]


def test_a_revoked_token_is_refused(client, token, db):
    db.execute(text("UPDATE scim_tokens SET revoked_at = now()"))
    db.commit()
    try:
        assert client.get("/scim/v2/Users", headers=token).status_code == 401
    finally:
        db.execute(text("UPDATE scim_tokens SET revoked_at = NULL"))
        db.commit()


def test_provisioning_cannot_reach_another_tenants_users(client, token):
    """The token is a credential for one tenant. Row-level security bounds it
    as well (ADR-0046), so this holds even if the query forgot."""
    r = client.get('/scim/v2/Users?filter=userName eq "owner@northwind.example"',
                   headers=token)
    assert r.json()["totalResults"] == 0

    assert client.get("/scim/v2/Users/USR_B_OWNER",
                      headers=token).status_code == 404


def test_provisioning_may_not_create_owners(client, db):
    """An IdP deciding who administers the tenant means anybody who can create
    an account there can administer this one. Refused at token creation."""
    r = client.post("/scim/tokens", headers=_owner(),
                    json={"name": "bad", "default_role": "owner"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "owner_not_allowed"


def test_the_last_owner_cannot_be_deprovisioned(client, token, db):
    """The same guard the API has (ADR-0048), reached by a different door. A
    provisioning integration that removes the last owner leaves a tenant nobody
    can administer, and the IdP has no idea it has done it."""
    r = client.patch("/scim/v2/Users/USR_A_OWNER", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "value": {"active": False}}],
    })
    assert r.status_code == 409
    assert r.json()["detail"]["scimType"] == "mutability"


def test_a_user_with_no_email_anywhere_is_refused(client, token):
    r = client.post("/scim/v2/Users", headers=token,
                    json={"schemas": [USER_SCHEMA], "userName": "no-at-sign"})
    assert r.status_code == 400
    assert r.json()["detail"]["scimType"] == "invalidValue"


def test_an_address_in_emails_is_accepted_when_username_is_a_login(client, token):
    """Some providers send a login name as `userName` and the address in
    `emails`. Both are legitimate and accounts are matched on the address."""
    r = client.post("/scim/v2/Users", headers=token, json={
        "schemas": [USER_SCHEMA], "userName": "jsmith",
        "emails": [{"value": "jsmith@kettle.example", "primary": True}]})
    assert r.status_code == 201
    assert r.json()["userName"] == "jsmith@kettle.example"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def test_the_service_provider_config_is_readable_without_a_token(client):
    """A provider reads it before it has been configured with a credential."""
    r = client.get("/scim/v2/ServiceProviderConfig")
    assert r.status_code == 200
    assert r.json()["patch"]["supported"] is True
    # Truthful `false` saves a support ticket.
    assert r.json()["bulk"]["supported"] is False
    assert r.json()["sort"]["supported"] is False


def test_groups_are_not_advertised(client):
    """Mapping an IdP's groups onto roles is a larger design. Saying so is
    better than letting a provider discover it by trying."""
    types = client.get("/scim/v2/ResourceTypes").json()
    assert {r["name"] for r in types["Resources"]} == {"User"}


def test_the_schema_only_advertises_attributes_we_actually_keep(client):
    """A schema listing attributes that are accepted and dropped is worse than
    a short one: a provider maps a field, sees no error, and believes it
    synced."""
    resources = client.get("/scim/v2/Schemas").json()["Resources"]
    names = {a["name"] for a in resources[0]["attributes"]}
    assert names == {"userName", "externalId", "active", "emails"}


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------
def test_a_provisioning_token_is_stored_as_a_hash(client, db):
    r = client.post("/scim/tokens", headers=_owner(), json={"name": "okta"})
    assert r.status_code == 201
    value = r.json()["token"]

    stored = db.execute(text("SELECT token_hash FROM scim_tokens WHERE id = :i"),
                        {"i": r.json()["id"]}).scalar()
    assert stored != value
    assert stored == scim.hash_token(value)


def test_the_token_is_never_shown_again(client):
    created = client.post("/scim/tokens", headers=_owner(), json={"name": "okta"})
    listed = client.get("/scim/tokens", headers=_owner()).json()["tokens"]
    assert created.json()["token"] not in str(listed)


def test_last_used_says_whether_the_integration_is_running(client, token):
    """The question asked when somebody's offboarding did not take effect."""
    client.get("/scim/v2/Users", headers=token)
    listed = client.get("/scim/tokens", headers=_owner()).json()["tokens"]
    assert any(t["last_used_at"] for t in listed)


def test_managing_provisioning_tokens_requires_the_owner_role(client):
    analyst = {"Authorization": f"Bearer {sec.issue_token('USR_A_ANALYST')}"}
    assert client.get("/scim/tokens", headers=analyst).status_code == 403
    assert client.post("/scim/tokens", headers=analyst, json={}).status_code == 403


# --------------------------------------------------------------------------
# Isolating the revocation
# --------------------------------------------------------------------------
def test_deactivating_through_scim_moves_credentials_valid_from(client, token, db):
    """The revocation, asserted directly rather than through its effect.

    Deactivating sets two things: the status, and `credentials_valid_from`. The
    status filter alone already stops the account, so a mutation that removed
    the revocation left every test passing -- the two controls hid each other.
    This one reaches for the revocation on its own, because it is what still
    stands if somebody adds a lookup that skips `authz.resolve`.
    """
    created = _create(client, token, email="revoked@kettle.example")
    user_id = created["id"]
    assert db.execute(text(
        "SELECT credentials_valid_from FROM users WHERE id = :i"),
        {"i": user_id}).scalar() is None

    client.patch(f"/scim/v2/Users/{user_id}", headers=token, json={
        "schemas": [scim.PATCH_SCHEMA],
        "Operations": [{"op": "replace", "value": {"active": False}}],
    })

    assert db.execute(text(
        "SELECT credentials_valid_from FROM users WHERE id = :i"),
        {"i": user_id}).scalar() is not None, (
        "deprovisioning disabled the account without revoking its tokens")
