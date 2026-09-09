#!/usr/bin/env python
"""Stand up a new customer: a tenant, its first merchant, and its first owner.

§45. Until this existed, a tenant and a merchant arrived only through
`seed_data.py`, which builds a demonstration dataset and drops the schema to do
it. There was no way to bring a real customer onto the platform at all.

## Why this is a script and not an endpoint

`POST /merchants` adds a merchant to a tenant that already has an owner, and an
owner is the authority for that. Creating a *tenant* is different in kind: it
mints the first owner of a tenant that has nobody in it yet, so there is no
principal inside the system who could authorise it. The highest role here is
`owner`, scoped to a merchant within a tenant.

An endpoint would need one of two things, and both are worse than a script:

  a platform superuser   a role this system does not have, which would sit
                         above every tenant boundary the rest of the design
                         spends its effort enforcing
  any owner may do it    then an owner mints tenants they control, which is
                         not onboarding, it is escalation

So it is an operator action, run with database credentials by whoever operates
the platform — and it writes an audit row saying who ran it, because "a tenant
appeared" should not be something only the row's `created_at` remembers.

## What it produces

    tenant                  the customer
    merchant                their first one; more via POST /merchants
    roles + permissions     owner, approver, analyst (ADR-0047)
    the first owner         with a token, printed once

## Usage

    python scripts/onboard_tenant.py --tenant "Northwind Holdings" \\
        --merchant "Northwind Traders" --owner-email ops@northwind.example

Add `--dry-run` to see what it would do without writing.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.audit.trace import record
from app.authz import ensure_default_roles
from app.db import session_scope


def _slug(prefix: str, name: str) -> str:
    """A readable id derived from the name, with a random tail.

    Readable because these appear in logs, support conversations and the URL of
    every screen; random-tailed because two customers may share a first word and
    an id collision at onboarding is a bad first day.
    """
    head = "".join(c for c in name.upper() if c.isalnum())[:10] or "NEW"
    return f"{prefix}_{head}_{uuid.uuid4().hex[:6].upper()}"


def onboard(session, *, tenant_name: str, merchant_name: str, owner_email: str,
            currency: str = "INR", actor: str = "operator") -> dict:
    """Create the tenant, its merchant, its roles and its first owner.

    One transaction. A tenant with no roles cannot have a first user, and a
    tenant with no owner is one nobody can administer — so a partial success
    here is worse than a failure, because it looks like a customer exists.
    """
    from app import lifecycle

    tenant_id = _slug("TEN", tenant_name)
    merchant_id = _slug("MERCH", merchant_name)

    session.execute(text(
        "INSERT INTO tenants (id, name, created_at) VALUES (:i, :n, now())"),
        {"i": tenant_id, "n": tenant_name})
    session.execute(text(
        "INSERT INTO merchants (id, tenant_id, name, currency, policy_config, "
        "                       created_at) "
        "VALUES (:i, :t, :n, :c, '{}', now())"),
        {"i": merchant_id, "t": tenant_id, "n": merchant_name, "c": currency})

    # Before the user, not after: `create_user` assigns a role by name and a
    # tenant with no roles cannot give one.
    ensure_default_roles(session, tenant_id)

    created = lifecycle.create_user(
        session,
        actor=SimpleNamespace(user_id=actor, tenant_id=tenant_id,
                              merchant_id=merchant_id, role="owner"),
        email=owner_email, role_name="owner")

    # Who did this, in the one table that cannot be edited afterwards. A tenant
    # that appeared with nothing but a `created_at` to explain it is exactly the
    # gap the trail exists to close.
    record(session, SimpleNamespace(id=None, merchant_id=merchant_id,
                                    user_id=created.user_id),
           "tenant_onboarded",
           {"tenant_id": tenant_id, "merchant_id": merchant_id,
            "tenant_name": tenant_name, "merchant_name": merchant_name,
            "first_owner": created.user_id, "by": actor})

    return {"tenant_id": tenant_id, "merchant_id": merchant_id,
            "owner_user_id": created.user_id, "owner_email": created.email,
            "token": getattr(created, "token", None)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tenant", required=True, help="the customer's name")
    ap.add_argument("--merchant", required=True, help="their first merchant")
    ap.add_argument("--owner-email", required=True, help="who administers it")
    ap.add_argument("--currency", default="INR")
    ap.add_argument("--actor", default="operator",
                    help="recorded in the audit trail as who ran this")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        print("would create:")
        print(f"  tenant    {args.tenant}")
        print(f"  merchant  {args.merchant} ({args.currency})")
        print(f"  owner     {args.owner_email}")
        print("  roles     owner, approver, analyst")
        print("\nNothing was written.")
        return 0

    with session_scope() as s:
        out = onboard(s, tenant_name=args.tenant, merchant_name=args.merchant,
                      owner_email=args.owner_email, currency=args.currency,
                      actor=args.actor)

    print(f"tenant     {out['tenant_id']}  {args.tenant}")
    print(f"merchant   {out['merchant_id']}  {args.merchant}")
    print(f"owner      {out['owner_user_id']}  {out['owner_email']}")
    if out["token"]:
        # Once. There is no retrieval path -- the database holds a digest.
        print(f"\ntoken      {out['token']}")
        print("\nThis token is shown once and cannot be recovered. Give it to the "
              "owner\nover something you would send a password over, and they can "
              "add\ncolleagues from the People screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
