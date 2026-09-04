"""Create a tenant, its first merchant, its roles and its first owner.

    PYTHONPATH=. python scripts/provision.py \
        --tenant TEN_ACME --tenant-name "Acme Group" \
        --merchant MERCH_ACME --merchant-name "Acme Retail" \
        --owner ops@acme.example

Deliberately a script and not an API route. Everything else about administering
people moved to the API in ADR-0048 -- creating users, moving them between
roles, offboarding leavers -- because those happen weekly and were costing an
engineer each time.

Creating a *tenant* is different. It is a platform operation, and exposing it
over the same API would require a principal standing outside every tenant: an
identity with authority over all customers, reachable with a bearer token. That
is a liability worth more than the convenience, so this stays a command somebody
runs deliberately and a reviewer can see in a shell history.

Idempotent on the ids, so a re-run after a partial failure completes rather than
duplicating.

The owner's bearer token is printed ONCE. Authentication is an HMAC of the user
id (ADR-0025), so there is no password to set -- the token IS the credential, and
nothing stores it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import session_scope
from app.lifecycle import LifecycleError, provision_tenant
from app.tenancy import unscoped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True, help="Tenant id, e.g. TEN_ACME")
    ap.add_argument("--tenant-name", required=True)
    ap.add_argument("--merchant", required=True, help="Merchant id, e.g. MERCH_ACME")
    ap.add_argument("--merchant-name", required=True)
    ap.add_argument("--owner", required=True, help="Email of the first owner")
    ap.add_argument("--currency", default="INR")
    args = ap.parse_args()

    # Unscoped: this creates the tenant that scoping would otherwise be relative
    # to, so there is nothing to be bound to yet.
    try:
        with unscoped(), session_scope() as s:
            owner = provision_tenant(
                s, tenant_id=args.tenant, tenant_name=args.tenant_name,
                merchant_id=args.merchant, merchant_name=args.merchant_name,
                owner_email=args.owner, currency=args.currency)
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"tenant    {args.tenant}  ({args.tenant_name})")
    print(f"merchant  {args.merchant}  ({args.merchant_name}, {args.currency})")
    print("roles     owner, analyst, approver")
    print(f"owner     {owner.user_id}  {owner.email}")
    print()
    print("Bearer token for the owner — shown once, stored nowhere:")
    print(f"  {owner.token}")
    print()
    print("They can now add the rest of their team:")
    print(f"  curl -X POST $BASE/api/users -H 'Authorization: Bearer {owner.token}' \\")
    print("       -H 'Content-Type: application/json' \\")
    print("       -d '{\"email\": \"analyst@example.com\", \"role\": \"analyst\"}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
