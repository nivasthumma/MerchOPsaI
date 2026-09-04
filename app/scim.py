"""SCIM 2.0 provisioning — RFC 7643 (schema) and RFC 7644 (protocol).

ADR-0050 let a customer's employees sign in through their identity provider. It
did not let the customer's IdP *tell us* when somebody leaves. Until it can, an
employee removed at Okta keeps working here until an owner remembers to disable
them, and "we deprovision through our IdP" is a sentence a customer says in a
security review that would not have been true.

That is the gap this closes, and deprovisioning is the operation everything else
here exists to support.

## What is implemented

`/scim/v2/Users` — list with a filter, create, read, replace, patch, delete —
plus the three discovery documents a provider reads before it will talk to you
at all (`ServiceProviderConfig`, `ResourceTypes`, `Schemas`).

**Groups are not.** Mapping an IdP's groups onto roles is a genuinely larger
design -- what happens to somebody in two groups, what happens when a group is
renamed, whether a group can grant `owner` -- and both Okta and Entra provision
users without it. Advertised as unsupported in `ServiceProviderConfig` rather
than left for a provider to discover by trying.

## Deprovisioning is a status, never a delete

`active: false`, and `DELETE`, both mean DISABLED. `audit_logs.user_id` and
`approval_signatures.user_id` point at the row (ADR-0048), and a trail that
disappears when somebody leaves the company is not a trail. RFC 7644 §3.6 allows
exactly this: a service provider MAY disable rather than remove, and a
subsequent GET returning 404 is what makes it look like a delete to the client.

## Why the request models ignore unknown attributes

Every response model in this application inherits `Contract`, which forbids
extra keys, so a field the server sends and does not declare is a test failure
rather than a surprise. SCIM *requests* are the other way round: Okta sends
`name`, `emails`, `phoneNumbers`, `meta`, `urn:ietf:params:scim:schemas:
extension:enterprise:2.0:User` and more, none of which this application models.
Rejecting a payload for containing attributes the standard says a client may
send would mean SCIM does not work, so requests ignore what they do not
recognise -- and that is a deliberate exception, not an oversight.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import text

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class ScimError(Exception):
    """A SCIM failure, carrying the status and `scimType` RFC 7644 §3.12 wants."""

    def __init__(self, detail: str, *, status: int = 400,
                 scim_type: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.scim_type = scim_type

    def as_dict(self) -> dict:
        body = {"schemas": [ERROR_SCHEMA], "status": str(self.status),
                "detail": self.detail}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


# --------------------------------------------------------------------------
# The provisioning credential
# --------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class ScimClient:
    token_id: str
    tenant_id: str
    default_merchant_id: str
    default_role: str


def mint_token(session, *, tenant_id: str, merchant_id: str, role: str,
               name: str, created_by: str) -> tuple[str, str]:
    """A new provisioning credential. Returns (token_id, the token itself).

    The token is returned once and stored as a hash. Anything else means a
    credential that can create accounts sitting in a readable column.
    """
    token = f"scim_{secrets.token_urlsafe(36)}"
    token_id = f"SCIM_{uuid.uuid4().hex[:12].upper()}"
    session.execute(text("""
        INSERT INTO scim_tokens (id, tenant_id, token_hash, name,
                                 default_merchant_id, default_role,
                                 created_at, created_by)
        VALUES (:i, :t, :h, :n, :m, :r, now(), :by)
    """), {"i": token_id, "t": tenant_id, "h": hash_token(token), "n": name,
           "m": merchant_id, "r": role, "by": created_by})
    session.flush()
    return token_id, token


def authenticate(session, token: str) -> ScimClient:
    """Resolve a provisioning token, or refuse.

    Looked up by hash, so the comparison is an indexed equality on a digest
    rather than a scan. A revoked token is refused with the same 401 as an
    unknown one: telling a caller that their token *used* to work is telling
    somebody who stole it that they found something real.
    """
    row = session.execute(text("""
        SELECT id, tenant_id, default_merchant_id, default_role, revoked_at
        FROM scim_tokens WHERE token_hash = :h
    """), {"h": hash_token(token or "")}).mappings().first()
    if row is None or row["revoked_at"] is not None:
        raise ScimError("Invalid provisioning token.", status=401)

    session.execute(text(
        "UPDATE scim_tokens SET last_used_at = now() WHERE id = :i"),
        {"i": row["id"]})
    return ScimClient(row["id"], row["tenant_id"], row["default_merchant_id"],
                      row["default_role"])


# --------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------
def to_scim(row) -> dict:
    """One user, as RFC 7643 §4.1 describes it."""
    created = row["created_at"]
    return {
        "schemas": [USER_SCHEMA],
        "id": row["id"],
        "externalId": row["external_id"],
        "userName": row["email"],
        "name": {"formatted": row["email"]},
        "emails": [{"value": row["email"], "primary": True, "type": "work"}],
        # The single most important attribute here. False is how an identity
        # provider says somebody has left.
        "active": row["status"] == "ACTIVE",
        "meta": {
            "resourceType": "User",
            "created": _iso(created),
            "lastModified": _iso(row["deactivated_at"] or created),
            "location": f"/scim/v2/Users/{row['id']}",
        },
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


_USER_COLUMNS = """
    u.id, u.email, u.external_id, u.status, u.created_at, u.deactivated_at,
    u.tenant_id, u.merchant_id
"""


def _fetch(session, client: ScimClient, user_id: str):
    row = session.execute(text(f"""
        SELECT {_USER_COLUMNS} FROM users u
        WHERE u.id = :i AND u.tenant_id = :t
    """), {"i": user_id, "t": client.tenant_id}).mappings().first()
    if row is None:
        raise ScimError(f"No user with id {user_id!r}.", status=404)
    return row


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------
#: The one filter every provider actually sends, and the only one supported.
#: Okta and Entra both use `userName eq "…"` to decide create-or-update, and
#: implementing the rest of RFC 7644 §3.4.2.2's grammar would be building a
#: query language for two clients that ask one question.
_FILTER = re.compile(r'^\s*(userName|externalId)\s+eq\s+"([^"]*)"\s*$', re.I)


def list_users(session, client: ScimClient, *, scim_filter: str | None = None,
               start_index: int = 1, count: int = 100) -> dict:
    params: dict = {"t": client.tenant_id}
    where = "u.tenant_id = :t"

    if scim_filter:
        match = _FILTER.match(scim_filter)
        if not match:
            raise ScimError(
                f"Only `userName eq \"…\"` and `externalId eq \"…\"` are "
                f"supported, not {scim_filter!r}.",
                status=400, scim_type="invalidFilter")
        attribute, value = match.group(1).lower(), match.group(2)
        if attribute == "username":
            where += " AND lower(u.email) = :v"
            params["v"] = value.lower()
        else:
            where += " AND u.external_id = :v"
            params["v"] = value

    total = session.execute(text(
        f"SELECT count(*) FROM users u WHERE {where}"), params).scalar()

    count = max(0, min(count, 200))
    params |= {"limit": count, "offset": max(0, start_index - 1)}
    rows = session.execute(text(f"""
        SELECT {_USER_COLUMNS} FROM users u WHERE {where}
        ORDER BY u.id LIMIT :limit OFFSET :offset
    """), params).mappings().all()

    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": int(total or 0),
        "startIndex": start_index,
        "itemsPerPage": len(rows),
        "Resources": [to_scim(r) for r in rows],
    }


def get_user(session, client: ScimClient, user_id: str) -> dict:
    return to_scim(_fetch(session, client, user_id))


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------
def _email_of(payload: dict) -> str:
    """`userName` if it looks like an address, else the primary email.

    Okta sends the address as `userName`; some providers send a login name and
    put the address in `emails`. Both are legitimate and the account is matched
    on an address, so this has to accept either.
    """
    user_name = str(payload.get("userName") or "").strip().lower()
    if "@" in user_name:
        return user_name
    emails = payload.get("emails") or []
    primary = next((e for e in emails if e.get("primary")), None)
    chosen = primary or (emails[0] if emails else None)
    return str((chosen or {}).get("value") or "").strip().lower()


def create_user(session, client: ScimClient, payload: dict) -> dict:
    email = _email_of(payload)
    if not email or "@" not in email:
        raise ScimError("No email address in `userName` or `emails`.",
                        status=400, scim_type="invalidValue")

    existing = session.execute(text("""
        SELECT id FROM users WHERE lower(email) = :e AND tenant_id = :t
    """), {"e": email, "t": client.tenant_id}).scalar()
    if existing:
        # RFC 7644 §3.3: 409 with scimType `uniqueness`. Providers rely on this
        # to switch from create to update rather than duplicating a person.
        raise ScimError(f"{email} already exists.", status=409,
                        scim_type="uniqueness")

    role = session.execute(text(
        "SELECT id, name FROM roles WHERE tenant_id = :t AND name = :n"),
        {"t": client.tenant_id, "n": client.default_role}).mappings().first()
    if role is None:
        raise ScimError(f"This token provisions users as "
                        f"{client.default_role!r}, and no such role exists.",
                        status=500)
    if role["name"] == "owner":
        # Same rule as SSO (ADR-0050): a provisioning integration deciding who
        # administers the tenant means anybody who can create an account at the
        # customer's IdP can administer this one.
        raise ScimError("Provisioning may not create owners.", status=403)

    user_id = f"USR_{uuid.uuid4().hex[:12].upper()}"
    active = payload.get("active", True)
    session.execute(text("""
        INSERT INTO users (id, tenant_id, merchant_id, email, role_id,
                           external_id, status, created_by, created_at)
        VALUES (:i, :t, :m, :e, :r, :x, :s, :by, now())
    """), {"i": user_id, "t": client.tenant_id, "m": client.default_merchant_id,
           "e": email, "r": role["id"],
           "x": payload.get("externalId"),
           "s": "ACTIVE" if active else "DISABLED",
           "by": f"scim:{client.token_id}"})
    session.flush()
    return to_scim(_fetch(session, client, user_id))


# --------------------------------------------------------------------------
# Updating
# --------------------------------------------------------------------------
def _set_active(session, client: ScimClient, row, active: bool) -> None:
    if active == (row["status"] == "ACTIVE"):
        return
    if active:
        session.execute(text("""
            UPDATE users SET status = 'ACTIVE', deactivated_at = NULL,
                             deactivated_by = NULL
            WHERE id = :i
        """), {"i": row["id"]})
        return

    _refuse_if_last_owner(session, row)
    session.execute(text("""
        UPDATE users SET status = 'DISABLED', deactivated_at = now(),
                         deactivated_by = :by
        WHERE id = :i
    """), {"i": row["id"], "by": f"scim:{client.token_id}"})

    # Their tokens go too (ADR-0049). Deprovisioning that leaves a live session
    # is deprovisioning in name only, and it is the whole reason this module
    # exists.
    from app import auth

    auth.revoke_all_for(session, row["id"])


def _refuse_if_last_owner(session, row) -> None:
    remaining = session.execute(text("""
        SELECT count(*) FROM users u JOIN roles r ON r.id = u.role_id
        WHERE u.merchant_id = :m AND r.name = 'owner'
          AND u.status = 'ACTIVE' AND u.id <> :u
    """), {"m": row["merchant_id"], "u": row["id"]}).scalar()
    if not remaining:
        # The same guard the API has (ADR-0048), reached by a different door. A
        # provisioning integration that removes the last owner leaves a tenant
        # nobody can administer, and the IdP has no idea it has done it.
        raise ScimError(
            "Refusing to deactivate the last active owner of "
            f"{row['merchant_id']}: it would leave nobody able to administer "
            "it. Promote somebody first.", status=409, scim_type="mutability")


def replace_user(session, client: ScimClient, user_id: str, payload: dict) -> dict:
    """PUT. The whole resource as the client believes it should be."""
    row = _fetch(session, client, user_id)
    _set_active(session, client, row, bool(payload.get("active", True)))

    if payload.get("externalId") is not None:
        session.execute(text("UPDATE users SET external_id = :x WHERE id = :i"),
                        {"x": payload["externalId"], "i": user_id})

    email = _email_of(payload)
    if email and email != row["email"]:
        clash = session.execute(text("""
            SELECT id FROM users
            WHERE lower(email) = :e AND tenant_id = :t AND id <> :i
        """), {"e": email, "t": client.tenant_id, "i": user_id}).scalar()
        if clash:
            raise ScimError(f"{email} belongs to another user.", status=409,
                            scim_type="uniqueness")
        session.execute(text("UPDATE users SET email = :e WHERE id = :i"),
                        {"e": email, "i": user_id})

    session.flush()
    return to_scim(_fetch(session, client, user_id))


def patch_user(session, client: ScimClient, user_id: str, payload: dict) -> dict:
    """PATCH, RFC 7644 §3.5.2. What Entra sends to deactivate somebody.

    Handles the attributes this application models -- `active`, `externalId`,
    `userName` -- and ignores operations on anything else rather than failing.
    A provider syncing a phone number should not have its deprovisioning
    rejected because of it.
    """
    row = _fetch(session, client, user_id)
    operations = payload.get("Operations") or payload.get("operations") or []
    if not isinstance(operations, list):
        raise ScimError("`Operations` must be a list.", status=400,
                        scim_type="invalidSyntax")

    for operation in operations:
        verb = str(operation.get("op") or "").lower()
        if verb not in ("add", "replace", "remove"):
            raise ScimError(f"Unsupported op {operation.get('op')!r}.",
                            status=400, scim_type="invalidSyntax")

        path = str(operation.get("path") or "").strip()
        value = operation.get("value")

        # A PATCH with no path carries an object of attributes. Entra sends
        # `{"op": "replace", "value": {"active": false}}` this way.
        changes = value if (not path and isinstance(value, dict)) else {path: value}

        for attribute, new_value in changes.items():
            name = attribute.split(":")[-1].lower()
            if name == "active":
                if verb == "remove":
                    continue
                _set_active(session, client, row,
                            new_value if isinstance(new_value, bool)
                            else str(new_value).lower() == "true")
                row = _fetch(session, client, user_id)
            elif name == "externalid":
                session.execute(
                    text("UPDATE users SET external_id = :x WHERE id = :i"),
                    {"x": None if verb == "remove" else new_value, "i": user_id})
            # Everything else is an attribute this application does not model.
            # Ignored on purpose: see the module docstring.

    session.flush()
    return to_scim(_fetch(session, client, user_id))


def delete_user(session, client: ScimClient, user_id: str) -> None:
    """DELETE, which deactivates.

    RFC 7644 §3.6 permits it: a service provider MAY disable rather than remove,
    and a subsequent GET returning 404 is what makes it a delete to the client.
    The row stays because the audit trail points at it.
    """
    row = _fetch(session, client, user_id)
    _set_active(session, client, row, False)
    session.flush()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def service_provider_config() -> dict:
    """What this server supports. Providers read it before anything else, and
    a truthful `supported: false` saves a support ticket."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken",
            "name": "Bearer token",
            "description": "A provisioning token issued by a tenant owner at "
                           "POST /scim/tokens. It is shown once.",
        }],
        "meta": {"resourceType": "ServiceProviderConfig",
                 "location": "/scim/v2/ServiceProviderConfig"},
    }


def resource_types() -> dict:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        # Groups are absent on purpose: mapping an IdP's groups onto roles is a
        # larger design, and both Okta and Entra provision users without it.
        "Resources": [{
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User", "name": "User", "endpoint": "/Users",
            "description": "A person who can sign in",
            "schema": USER_SCHEMA,
            "meta": {"resourceType": "ResourceType",
                     "location": "/scim/v2/ResourceTypes/User"},
        }],
    }


def schemas() -> dict:
    """Only the attributes this application actually holds.

    A schema advertising attributes that are accepted and dropped is worse than
    a short one: a provider maps a field, sees no error, and believes it synced.
    """
    def attribute(name, description, **kw):
        return {"name": name, "type": kw.get("type", "string"),
                "multiValued": kw.get("multiValued", False),
                "description": description,
                "required": kw.get("required", False),
                "caseExact": False, "mutability": kw.get("mutability", "readWrite"),
                "returned": "default", "uniqueness": kw.get("uniqueness", "none")}

    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [{
            "id": USER_SCHEMA,
            "name": "User",
            "description": "A person who can sign in to MerchantOps",
            "attributes": [
                attribute("userName", "The person's email address", required=True,
                          uniqueness="server"),
                attribute("externalId", "The identity provider's own id"),
                attribute("active", "False deactivates the account and revokes "
                                    "every token it holds", type="boolean"),
                attribute("emails", "Email addresses; the primary one is used",
                          type="complex", multiValued=True),
            ],
            "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{USER_SCHEMA}"},
        }],
    }
