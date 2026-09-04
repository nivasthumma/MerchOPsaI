"""Roles, permissions, and the one query that resolves a principal.

Permissions used to be a JSON list on `users`. That works, and it cannot answer
"who can approve a CRITICAL refund?" without reading every row and parsing every
list. It cannot be versioned, cannot be defined per tenant, and cannot produce
the access-review evidence an auditor asks for quarterly -- which is a
compliance gap wearing the costume of a schema decision.

Three tables replace it: a `permissions` catalogue, tenant-owned `roles`, and
the `role_permissions` join. A user has one role, as this system has always
modelled it.

## The catalogue is derived, not maintained

`CATALOGUE` is built from `app.tools.registry` -- every permission a registered
tool declares -- plus the reads that no single tool owns. Maintaining a second
hand-written list beside the one the policy engine gates on is how a tool comes
to require `action:refunds` while the catalogue offers `action:refund`, and
nobody finds out until somebody tries to use it.

## Where the authority lives

`resolve` is the only place that answers "what may this user do", and both
callers that need it -- the API's `current_principal` and the worker claiming a
queued task -- go through it. That is deliberate: authority read in two places
is authority that will eventually be read two ways.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

#: Permissions that exist independently of any one tool. Reads are grouped
#: rather than per-tool because a role that can see payments can see orders --
#: splitting them would produce permissions nobody would ever assign apart.
_STANDING = {
    "read:metrics": "Read revenue, failure and recovery figures",
    "read:orders": "Read orders, payments, refunds and customers",
}

#: What each default role grants. The names match what this system has used
#: since the first seed; the contents are what those users already held.
DEFAULT_ROLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "owner": ("Full authority over this merchant, including moving money",
              ("read:metrics", "read:orders", "action:refund", "action:recover")),
    "analyst": ("Read-only. Can investigate; cannot act",
                ("read:metrics", "read:orders")),
    # Exists because §25's dual approval needs two people who can each approve.
    # With one approver per merchant the control could only be demonstrated by
    # the same person signing twice, which is the thing it forbids.
    "approver": ("Can investigate and approve actions, including refunds",
                 ("read:metrics", "read:orders", "action:refund", "action:recover")),
}


def catalogue() -> dict[str, str]:
    """Every permission that exists, derived from what the tools require."""
    from app.tools.registry import REGISTRY

    out = dict(_STANDING)
    for name, spec in sorted(REGISTRY.items()):
        for perm in getattr(spec, "required_permissions", ()) or ():
            out.setdefault(perm, f"Required by `{name}` and tools like it")
    return out


# --------------------------------------------------------------------------
# Resolving a principal
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PrincipalRow:
    user_id: str
    tenant_id: str
    merchant_id: str
    email: str
    role: str
    permissions: list[str]
    status: str = "ACTIVE"

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"


# Split rather than `.format`ed: the SQL contains `'{}'` -- an empty text array,
# which is what a role with no permissions aggregates to -- and `str.format`
# reads that as a replacement field. It raised on the first call.
_RESOLVE_HEAD = """
    SELECT u.id, u.tenant_id, u.merchant_id, u.email, u.status, r.name AS role,
           coalesce(
               array_agg(rp.permission_name ORDER BY rp.permission_name)
                   FILTER (WHERE rp.permission_name IS NOT NULL),
               '{}'::text[]) AS permissions
    FROM users u
    JOIN roles r ON r.id = u.role_id
    LEFT JOIN role_permissions rp ON rp.role_id = r.id
    WHERE """
_RESOLVE_TAIL = """
    GROUP BY u.id, u.tenant_id, u.merchant_id, u.email, u.status, r.name
"""


def resolve(session, user_id: str, *, active_only: bool = True) -> PrincipalRow | None:
    """What this user may do, right now.

    `active_only` by default, so an offboarded account stops working the moment
    it is disabled rather than at its next login -- there is no session to expire
    and the bearer token stays valid forever (ADR-0025), so the row is the only
    thing that can revoke it. Pass False to look one up for administration,
    where a disabled user is exactly who you are asking about.

    A LEFT JOIN on permissions, so a role granting none resolves to a principal
    holding none rather than to no principal at all. Those are different facts:
    one is a user who can do nothing, the other is a user who does not exist,
    and the API answers 403 to one and 401 to the other.
    """
    predicate = "u.id = :u" + (" AND u.status = 'ACTIVE'" if active_only else "")
    row = session.execute(
        text(_RESOLVE_HEAD + predicate + _RESOLVE_TAIL), {"u": user_id}
    ).mappings().first()
    return _row(row) if row else None


def holders(session, *, tenant_id: str, merchant_id: str,
            required: list[str] | None = None,
            active_only: bool = True) -> list[PrincipalRow]:
    """Everyone attached to this merchant, optionally filtered to those holding
    every permission in `required`.

    This is the query that was impossible before: "who can approve a CRITICAL
    refund?" is `holders(..., required=["action:refund"])`.
    """
    predicate = "u.tenant_id = :t AND u.merchant_id = :m"
    if active_only:
        # A disabled user holding `action:refund` is not somebody who can
        # approve a refund, and a notification routed to them is a notification
        # nobody reads.
        predicate += " AND u.status = 'ACTIVE'"
    rows = session.execute(
        text(_RESOLVE_HEAD + predicate + _RESOLVE_TAIL + " ORDER BY u.id"),
        {"t": tenant_id, "m": merchant_id},
    ).mappings().all()
    out = [_row(r) for r in rows]
    if required:
        need = set(required)
        out = [p for p in out if need.issubset(set(p.permissions))]
    return out


def _row(row) -> PrincipalRow:
    return PrincipalRow(
        user_id=row["id"], tenant_id=row["tenant_id"], merchant_id=row["merchant_id"],
        email=row["email"], role=row["role"], permissions=list(row["permissions"]),
        status=row["status"])


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
def ensure_catalogue(session) -> int:
    """Write any permission the tools require and the catalogue lacks."""
    written = 0
    for name, description in catalogue().items():
        result = session.execute(text("""
            INSERT INTO permissions (name, description, created_at)
            VALUES (:n, :d, now()) ON CONFLICT (name) DO NOTHING
        """), {"n": name, "d": description})
        written += result.rowcount or 0
    session.flush()
    return written


def ensure_default_roles(session, tenant_id: str) -> dict[str, str]:
    """Give a tenant the standard roles. Idempotent, and returns name -> id.

    Called by the seeder today and by tenant provisioning when that exists. A
    tenant with no roles is a tenant whose first user cannot be given one, so
    this is part of creating a tenant rather than a separate step somebody
    remembers.
    """
    ensure_catalogue(session)
    out: dict[str, str] = {}
    for name, (description, permissions) in DEFAULT_ROLES.items():
        role_id = session.execute(text("""
            INSERT INTO roles (id, tenant_id, name, description, created_at, updated_at)
            VALUES (:i, :t, :n, :d, now(), now())
            ON CONFLICT (tenant_id, name) DO UPDATE SET description = EXCLUDED.description
            RETURNING id
        """), {"i": f"ROLE_{uuid.uuid4().hex[:12].upper()}", "t": tenant_id,
               "n": name, "d": description}).scalar()
        out[name] = role_id
        for permission in permissions:
            session.execute(text("""
                INSERT INTO role_permissions (role_id, permission_name, granted_at)
                VALUES (:r, :p, now()) ON CONFLICT DO NOTHING
            """), {"r": role_id, "p": permission})
    session.flush()
    return out


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------
def access_review(session, *, tenant_id: str | None = None) -> list[dict]:
    """Who holds what, as a list somebody can sign off.

    The artefact a SOC 2 access review asks for quarterly. It was previously
    produced by reading `users.permissions` out of the database by hand, which
    is why it was never produced.

    Includes DISABLED accounts, deliberately. "Who still has access" and "whose
    access was removed, and when" are both questions a review asks, and an
    offboarded account missing from the list is indistinguishable from one that
    never existed.
    """
    rows = session.execute(text("""
        SELECT u.id AS user_id, u.email, u.tenant_id, u.merchant_id,
               u.status, u.created_at, u.deactivated_at,
               r.name AS role,
               coalesce(array_agg(rp.permission_name ORDER BY rp.permission_name)
                   FILTER (WHERE rp.permission_name IS NOT NULL), '{}') AS permissions
        FROM users u
        JOIN roles r ON r.id = u.role_id
        LEFT JOIN role_permissions rp ON rp.role_id = r.id
        WHERE (:t IS NULL OR u.tenant_id = :t)
        GROUP BY u.id, u.email, u.tenant_id, u.merchant_id, u.status,
                 u.created_at, u.deactivated_at, r.name
        ORDER BY u.tenant_id, u.merchant_id, u.id
    """), {"t": tenant_id}).mappings().all()
    return [dict(r) | {"permissions": list(r["permissions"])} for r in rows]
