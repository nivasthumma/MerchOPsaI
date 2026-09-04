"""Roles and permissions as tables — ADR-0047.

Permissions were a JSON list on `users`. It worked, and it could not answer
"who can approve a CRITICAL refund?" without reading every row and parsing every
list; could not be versioned; could not be defined per tenant; and could not
produce the access-review evidence an auditor asks for quarterly.

The first test here is that question, asked as a query.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import authz
from app.policy.engine import required_permissions
from app.tenancy import unscoped


# --------------------------------------------------------------------------
# The question that was impossible
# --------------------------------------------------------------------------
def test_who_can_approve_a_refund_is_a_query(db):
    who = authz.holders(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                        required=["action:refund"])
    assert {p.user_id for p in who} == {"USR_A_OWNER", "USR_A_APPROVER"}
    assert all(p.role in {"owner", "approver"} for p in who)


def test_the_analyst_is_not_among_them(db):
    who = authz.holders(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                        required=["action:refund"])
    assert "USR_A_ANALYST" not in {p.user_id for p in who}


def test_holders_does_not_cross_a_merchant_boundary(db):
    a = authz.holders(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A")
    b = authz.holders(db, tenant_id="TEN_NORTHWIND", merchant_id="MERCH_B")
    assert {p.user_id for p in a}.isdisjoint({p.user_id for p in b})


def test_the_wrong_tenant_returns_nobody(db):
    assert authz.holders(db, tenant_id="TEN_NORTHWIND", merchant_id="MERCH_A") == []


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------
def test_resolve_returns_what_the_role_grants(db):
    p = authz.resolve(db, "USR_A_OWNER")
    assert p.role == "owner"
    assert set(p.permissions) == {"read:metrics", "read:orders",
                                  "action:refund", "action:recover"}


def test_an_unknown_user_resolves_to_nothing(db):
    assert authz.resolve(db, "USR_NOBODY") is None


def test_a_role_with_no_permissions_is_a_principal_holding_none(db):
    """Different from a user who does not exist. The API answers 403 to one and
    401 to the other, and a LEFT JOIN is what keeps them distinguishable."""
    db.execute(text("""
        DELETE FROM role_permissions rp USING roles r, users u
        WHERE rp.role_id = r.id AND r.id = u.role_id AND u.id = 'USR_A_ANALYST'
    """))
    db.flush()
    p = authz.resolve(db, "USR_A_ANALYST")
    assert p is not None
    assert p.permissions == []


def test_revoking_from_a_role_revokes_for_everybody_holding_it(db):
    """The point of roles. Editing a JSON column changed one person; deleting a
    row here changes everybody with that role, which is what a revocation is."""
    before = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", required=["action:refund"])}
    assert "USR_A_OWNER" in before

    db.execute(text("""
        DELETE FROM role_permissions rp USING roles r
        WHERE rp.role_id = r.id AND r.tenant_id = 'TEN_KETTLE'
          AND r.name = 'owner' AND rp.permission_name = 'action:refund'
    """))
    db.flush()

    after = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", required=["action:refund"])}
    assert "USR_A_OWNER" not in after
    assert "USR_A_APPROVER" in after, "only the owner role was touched"


# --------------------------------------------------------------------------
# The catalogue is derived
# --------------------------------------------------------------------------
def test_every_permission_a_tool_requires_exists_in_the_catalogue(db):
    """A tool demanding `action:refunds` where the catalogue offers
    `action:refund` is a tool nobody can ever invoke, discovered the first time
    somebody tries. This is that discovery, moved earlier."""
    from app.tools.registry import REGISTRY

    with unscoped():
        known = {r[0] for r in db.execute(text("SELECT name FROM permissions"))}

    required = {p for spec in REGISTRY.values()
                for p in (getattr(spec, "required_permissions", ()) or ())}
    missing = required - known
    assert missing == set(), (
        f"tools require {sorted(missing)}, which no permission row grants")


def test_the_catalogue_is_built_from_the_registry_not_a_second_list():
    from app.tools.registry import REGISTRY

    required = {p for spec in REGISTRY.values()
                for p in (getattr(spec, "required_permissions", ()) or ())}
    assert required <= set(authz.catalogue())


@pytest.mark.parametrize("tool", ["request_refund", "generate_payment_link"])
def test_a_tools_permission_is_held_by_somebody(db, tool):
    """A permission no role grants is a tool that cannot be used. Worth failing
    on: it means either the tool or the default roles are wrong."""
    who = authz.holders(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                        required=required_permissions(tool))
    assert who, f"nobody at MERCH_A can invoke {tool}"


# --------------------------------------------------------------------------
# Provisioning and evidence
# --------------------------------------------------------------------------
def test_default_roles_are_idempotent(db):
    first = authz.ensure_default_roles(db, "TEN_KETTLE")
    second = authz.ensure_default_roles(db, "TEN_KETTLE")
    assert first == second, "re-provisioning a tenant must not mint new roles"


def test_roles_belong_to_one_tenant(db):
    kettle = authz.ensure_default_roles(db, "TEN_KETTLE")
    northwind = authz.ensure_default_roles(db, "TEN_NORTHWIND")
    assert set(kettle) == set(northwind), "same names"
    assert set(kettle.values()).isdisjoint(northwind.values()), (
        "different rows -- a tenant editing `owner` must not edit everybody's")


def test_the_access_review_lists_everyone_and_what_they_hold(db):
    review = authz.access_review(db)
    by_user = {r["user_id"]: r for r in review}
    assert set(by_user) >= {"USR_A_OWNER", "USR_A_ANALYST", "USR_A_APPROVER",
                            "USR_B_OWNER"}
    assert by_user["USR_A_ANALYST"]["permissions"] == ["read:metrics", "read:orders"]
    assert by_user["USR_A_OWNER"]["role"] == "owner"


def test_the_access_review_can_be_scoped_to_one_tenant(db):
    review = authz.access_review(db, tenant_id="TEN_NORTHWIND")
    assert {r["user_id"] for r in review} == {"USR_B_OWNER"}


# --------------------------------------------------------------------------
# Isolating the status filter
# --------------------------------------------------------------------------
def test_resolve_refuses_a_disabled_account_on_its_own(db):
    """`resolve` filtering on status, tested WITHOUT the token revocation that
    normally also stops a disabled user.

    Both controls fire on offboarding (ADR-0048 disables, ADR-0049 revokes), and
    together they hid each other: a mutation that removed the status filter left
    every test passing, because the revocation caught it. Defence in depth is
    the right design and it makes each layer untestable through the front door,
    so this reaches for one of them directly.
    """
    from sqlalchemy import text

    db.execute(text("UPDATE users SET status = 'DISABLED' WHERE id = 'USR_A_ANALYST'"))
    db.flush()

    assert authz.resolve(db, "USR_A_ANALYST") is None, (
        "a DISABLED user resolved to a principal; offboarding relies on this "
        "filter and on nothing else in this function")
    # Still findable when you are asking *about* them, which is what
    # administration needs.
    assert authz.resolve(db, "USR_A_ANALYST", active_only=False) is not None


def test_holders_excludes_a_disabled_account_on_its_own(db):
    """The same filter on the other query. A disabled user holding
    `action:refund` is not somebody who can approve one, and a notification
    routed to them is one nobody reads."""
    from sqlalchemy import text

    db.execute(text("UPDATE users SET status = 'DISABLED' WHERE id = 'USR_A_APPROVER'"))
    db.flush()

    live = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", required=["action:refund"])}
    assert "USR_A_APPROVER" not in live
    everyone = {p.user_id for p in authz.holders(
        db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A", active_only=False)}
    assert "USR_A_APPROVER" in everyone
