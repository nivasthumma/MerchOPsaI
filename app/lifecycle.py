"""Creating, moving and offboarding the people who use this system.

There was no path from "a customer signs a contract" to "their team can log in"
that did not involve an engineer with database access. Users existed because
`scripts/seed_data.py` inserted four of them; a leaver was offboarded with an
`UPDATE`; and a promotion was somebody editing a JSON column. That is a finding
an auditor writes up before they read any of the controls it protects, because
joiners-movers-leavers is the process SOC 2 spends most of its time on.

## What is API and what is not

**Within a tenant, its owner administers its people.** Creating a user, changing
a role, offboarding a leaver, defining a role: those are the operations that
happen weekly, and they are the ones that were costing an engineer.

**Creating a tenant is not.** It is a platform operation, and exposing it over
the same API would require inventing a principal that stands outside every
tenant -- an identity with authority over all customers, reachable with a bearer
token. That is a liability worth more than the convenience, so provisioning a
tenant is `scripts/provision.py`: audited, reviewable, and run deliberately.

## The guards

Two mistakes are easy, permanent, and made by tired people at the end of a
Friday, so they are refused rather than warned about:

- **Offboarding the last owner** leaves a merchant nobody can administer, with
  no way back that does not involve the database again.
- **Deleting a role somebody holds** would either orphan them or silently strip
  their authority, depending on which constraint fired first.

Everything here is audited, and every write happens inside the caller's tenant
so row-level security (ADR-0046) bounds it independently of these checks.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app import authz
from app.models import UserStatus

#: Deliberately loose. Address validity is the mail server's opinion, not ours,
#: and a regex that rejects a legitimate address is worse than one that admits a
#: bad one -- the second bounces, the first cannot be onboarded at all.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LifecycleError(Exception):
    """A lifecycle operation that must not proceed. Carries a machine code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CreatedUser:
    user_id: str
    email: str
    role: str
    #: A bearer token for the new user, returned ONCE and never stored.
    #:
    #: This is not an invitation flow. Authentication is an HMAC of the user id
    #: (ADR-0025's honest limitation), so there is no password to set and no
    #: acceptance step to complete -- creating the user IS granting the
    #: credential. A real invitation, with an expiring link the person redeems
    #: themselves, arrives with an identity provider and not before.
    token: str


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def create_user(session, *, actor, email: str, role_name: str,
                merchant_id: str | None = None) -> CreatedUser:
    """Add a person to the actor's merchant.

    The role is named, not passed as an id: an id is a value a caller could
    copy from another tenant's response, and while row-level security would
    refuse the write, a 500 from a foreign key is a worse answer than a 404 that
    says the role does not exist here.
    """
    from app.api.security import issue_token

    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise LifecycleError(f"{email!r} is not an email address.", "invalid_email")

    merchant_id = merchant_id or actor.merchant_id
    if merchant_id != actor.merchant_id:
        # A tenant may own several merchants, and administering a sibling is a
        # tenant-level operation this API deliberately does not offer.
        raise LifecycleError(
            "Users can only be created for your own merchant.", "wrong_merchant")

    role = _role_by_name(session, actor.tenant_id, role_name)

    existing = session.execute(text("""
        SELECT id, status FROM users WHERE lower(email) = :e AND merchant_id = :m
    """), {"e": email, "m": merchant_id}).mappings().first()
    if existing:
        raise LifecycleError(
            f"{email} already has an account here ({existing['status']}). "
            f"Re-enable it rather than creating a second.", "already_exists")

    user_id = f"USR_{uuid.uuid4().hex[:12].upper()}"
    session.execute(text("""
        INSERT INTO users (id, tenant_id, merchant_id, email, role_id, status,
                           created_by, created_at)
        VALUES (:i, :t, :m, :e, :r, 'ACTIVE', :by, now())
    """), {"i": user_id, "t": actor.tenant_id, "m": merchant_id, "e": email,
           "r": role["id"], "by": actor.user_id})
    session.flush()
    return CreatedUser(user_id, email, role["name"], issue_token(user_id))


def change_role(session, *, actor, user_id: str, role_name: str) -> dict:
    """Move somebody between roles. The mover half of joiners-movers-leavers."""
    user = _user_in_scope(session, actor, user_id)
    role = _role_by_name(session, actor.tenant_id, role_name)

    if user["role_id"] == role["id"]:
        return {"user_id": user_id, "role": role["name"], "changed": False}

    # Demoting the last owner strands the merchant exactly as deactivating them
    # would, and is the same mistake wearing a different verb.
    if user["role_name"] == "owner" and role["name"] != "owner":
        _refuse_if_last_owner(session, user, verb="demote")

    session.execute(text("UPDATE users SET role_id = :r WHERE id = :u"),
                    {"r": role["id"], "u": user_id})
    session.flush()
    return {"user_id": user_id, "role": role["name"], "changed": True,
            "previous_role": user["role_name"]}


def deactivate_user(session, *, actor, user_id: str) -> dict:
    """Offboard. The row stays; the account stops working.

    Never a delete. `audit_logs.user_id` and `approval_signatures.user_id` point
    at this row, and a trail that disappears when somebody leaves the company is
    not a trail.
    """
    user = _user_in_scope(session, actor, user_id)
    if user["status"] == UserStatus.DISABLED.value:
        return {"user_id": user_id, "status": "DISABLED", "changed": False}

    if user["role_name"] == "owner":
        _refuse_if_last_owner(session, user, verb="deactivate")

    session.execute(text("""
        UPDATE users SET status = 'DISABLED', deactivated_at = now(),
                         deactivated_by = :by
        WHERE id = :u
    """), {"u": user_id, "by": actor.user_id})
    session.flush()
    return {"user_id": user_id, "status": "DISABLED", "changed": True}


def reactivate_user(session, *, actor, user_id: str) -> dict:
    user = _user_in_scope(session, actor, user_id)
    if user["status"] == UserStatus.ACTIVE.value:
        return {"user_id": user_id, "status": "ACTIVE", "changed": False}
    session.execute(text("""
        UPDATE users SET status = 'ACTIVE', deactivated_at = NULL,
                         deactivated_by = NULL
        WHERE id = :u
    """), {"u": user_id})
    session.flush()
    return {"user_id": user_id, "status": "ACTIVE", "changed": True}


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
def create_role(session, *, actor, name: str, description: str,
                permissions: list[str]) -> dict:
    name = (name or "").strip()
    if not name:
        raise LifecycleError("A role needs a name.", "invalid_name")

    _check_permissions_exist(session, permissions)

    existing = session.execute(text(
        "SELECT id FROM roles WHERE tenant_id = :t AND name = :n"),
        {"t": actor.tenant_id, "n": name}).first()
    if existing:
        raise LifecycleError(f"A role named {name!r} already exists.", "already_exists")

    role_id = f"ROLE_{uuid.uuid4().hex[:12].upper()}"
    session.execute(text("""
        INSERT INTO roles (id, tenant_id, name, description, created_at, updated_at)
        VALUES (:i, :t, :n, :d, now(), now())
    """), {"i": role_id, "t": actor.tenant_id, "n": name, "d": description or ""})
    _set_permissions(session, role_id, permissions)
    return {"id": role_id, "name": name, "permissions": sorted(set(permissions))}


def set_role_permissions(session, *, actor, role_name: str,
                         permissions: list[str]) -> dict:
    """Replace what a role grants. Applies to everybody holding it, at once,
    which is the entire reason roles are rows (ADR-0047)."""
    role = _role_by_name(session, actor.tenant_id, role_name)
    _check_permissions_exist(session, permissions)

    before = [r[0] for r in session.execute(text(
        "SELECT permission_name FROM role_permissions WHERE role_id = :r ORDER BY 1"),
        {"r": role["id"]})]
    _set_permissions(session, role["id"], permissions)
    session.execute(text("UPDATE roles SET updated_at = now() WHERE id = :r"),
                    {"r": role["id"]})
    session.flush()
    after = sorted(set(permissions))
    return {"id": role["id"], "name": role["name"], "permissions": after,
            "granted": sorted(set(after) - set(before)),
            "revoked": sorted(set(before) - set(after))}


def delete_role(session, *, actor, role_name: str) -> dict:
    role = _role_by_name(session, actor.tenant_id, role_name)
    holders = session.execute(text(
        "SELECT count(*) FROM users WHERE role_id = :r"), {"r": role["id"]}).scalar()
    if holders:
        raise LifecycleError(
            f"{holders} user(s) hold {role_name!r}. Move them to another role "
            f"first -- deleting it would either orphan them or silently strip "
            f"their authority.", "role_in_use")
    session.execute(text("DELETE FROM roles WHERE id = :r"), {"r": role["id"]})
    session.flush()
    return {"name": role_name, "deleted": True}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _role_by_name(session, tenant_id: str, name: str) -> dict:
    row = session.execute(text(
        "SELECT id, name FROM roles WHERE tenant_id = :t AND name = :n"),
        {"t": tenant_id, "n": (name or "").strip()}).mappings().first()
    if row is None:
        available = [r[0] for r in session.execute(text(
            "SELECT name FROM roles WHERE tenant_id = :t ORDER BY name"),
            {"t": tenant_id})]
        raise LifecycleError(
            f"No role named {name!r} in this tenant. Available: {available}.",
            "unknown_role")
    return dict(row)


def _user_in_scope(session, actor, user_id: str) -> dict:
    """The user, if the actor may administer them.

    Row-level security already bounds this to the actor's merchant, so the
    lookup returning nothing means either "no such user" or "not yours" -- and
    the answer is the same 404 either way, because distinguishing them tells a
    caller whether an id exists in somebody else's tenant.
    """
    row = session.execute(text("""
        SELECT u.id, u.status, u.merchant_id, u.role_id, r.name AS role_name
        FROM users u JOIN roles r ON r.id = u.role_id
        WHERE u.id = :u AND u.merchant_id = :m
    """), {"u": user_id, "m": actor.merchant_id}).mappings().first()
    if row is None:
        raise LifecycleError("Unknown user.", "unknown_user")
    return dict(row)


def _refuse_if_last_owner(session, user: dict, *, verb: str) -> None:
    remaining = session.execute(text("""
        SELECT count(*) FROM users u JOIN roles r ON r.id = u.role_id
        WHERE u.merchant_id = :m AND r.name = 'owner'
          AND u.status = 'ACTIVE' AND u.id <> :u
    """), {"m": user["merchant_id"], "u": user["id"]}).scalar()
    if not remaining:
        raise LifecycleError(
            f"Refusing to {verb} the last active owner of {user['merchant_id']}. "
            f"That would leave nobody able to administer it, and no way back "
            f"that does not involve the database. Promote somebody first.",
            "last_owner")


def _check_permissions_exist(session, permissions: list[str]) -> None:
    """A permission not in the catalogue grants nothing and looks like it does.

    The catalogue is derived from the tool registry (ADR-0047), so a typo here
    produces a role that appears to authorise something and authorises nothing --
    discovered when somebody is refused an action they were told they could take.
    """
    if not permissions:
        return
    known = {r[0] for r in session.execute(text("SELECT name FROM permissions"))}
    unknown = sorted(set(permissions) - known)
    if unknown:
        raise LifecycleError(
            f"No such permission(s): {unknown}. Known: {sorted(known)}.",
            "unknown_permission")


def _set_permissions(session, role_id: str, permissions: list[str]) -> None:
    session.execute(text("DELETE FROM role_permissions WHERE role_id = :r"),
                    {"r": role_id})
    for permission in sorted(set(permissions)):
        session.execute(text("""
            INSERT INTO role_permissions (role_id, permission_name, granted_at)
            VALUES (:r, :p, now())
        """), {"r": role_id, "p": permission})
    session.flush()


# --------------------------------------------------------------------------
# Provisioning a tenant — not an API operation
# --------------------------------------------------------------------------
def provision_tenant(session, *, tenant_id: str, tenant_name: str,
                     merchant_id: str, merchant_name: str, owner_email: str,
                     currency: str = "INR") -> CreatedUser:
    """Create a tenant, its first merchant, its roles, and its first owner.

    Deliberately not reachable over HTTP. It creates a tenant, which is a
    platform operation: exposing it would need a principal standing outside
    every tenant, and an identity with authority over all customers reachable
    with a bearer token is a liability worth more than the convenience.

    Idempotent on the ids, so a re-run after a partial failure completes rather
    than duplicating.
    """
    from app.api.security import issue_token

    session.execute(text("""
        INSERT INTO tenants (id, name, created_at) VALUES (:i, :n, now())
        ON CONFLICT (id) DO NOTHING
    """), {"i": tenant_id, "n": tenant_name})
    session.execute(text("""
        INSERT INTO merchants (id, tenant_id, name, currency, policy_config, created_at)
        VALUES (:i, :t, :n, :c, '{}'::json, now())
        ON CONFLICT (id) DO NOTHING
    """), {"i": merchant_id, "t": tenant_id, "n": merchant_name, "c": currency})

    roles = authz.ensure_default_roles(session, tenant_id)

    email = (owner_email or "").strip().lower()
    if not _EMAIL.match(email):
        raise LifecycleError(f"{email!r} is not an email address.", "invalid_email")

    existing = session.execute(text(
        "SELECT id FROM users WHERE lower(email) = :e AND merchant_id = :m"),
        {"e": email, "m": merchant_id}).scalar()
    user_id = existing or f"USR_{uuid.uuid4().hex[:12].upper()}"
    if not existing:
        session.execute(text("""
            INSERT INTO users (id, tenant_id, merchant_id, email, role_id, status,
                               created_by, created_at)
            VALUES (:i, :t, :m, :e, :r, 'ACTIVE', 'provisioning', now())
        """), {"i": user_id, "t": tenant_id, "m": merchant_id, "e": email,
               "r": roles["owner"]})
    session.flush()
    return CreatedUser(user_id, email, "owner", issue_token(user_id))


def utcnow() -> datetime:
    return datetime.now(UTC)
