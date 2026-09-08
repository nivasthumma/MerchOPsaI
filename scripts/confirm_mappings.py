#!/usr/bin/env python3
"""Confirm each synthetic->provider mapping against the provider itself.

README known limitation: `provider_mappings.verified_at` is null on every
seeded row, and null means *nobody has checked* -- deliberately a different
claim from "checked and it was there". Nothing ever set it, and there was no
command that could: the column existed, `record_mapping` accepted the value,
and the value was never produced.

This is that command. It reads each mapped payment back through the adapter
and stamps `verified_at` only where the provider agrees.

## Why the amount is compared and not just the existence

A mapping that resolves to a real payment belonging to somebody else is worse
than a mapping that resolves to nothing: the refund succeeds, the money moves,
and every check upstream passes because the id was valid. So existence is not
the test. The amount must match too, and a mismatch is reported and left
UNVERIFIED rather than stamped with a caveat.

## Against the mock this proves almost nothing, and says so

The mock adapter's `get_payment` reads `payments` -- OUR table. So comparing a
mapping against it compares our record with our record, and it agrees by
construction. I checked rather than assumed: bumping a payment's recorded
amount by 100 and re-running still reported "21 confirmed, 0 mismatched",
because the same UPDATE moved both sides.

That makes the mock run a test of this SCRIPT and of the mapping LAYER --
resolve, external id, round-trip -- and not a confirmation of anything.
Reporting it as "confirmed" would be the exact dishonesty
`provider_mappings.verified_at` exists to prevent, so against the mock it says
`round-tripped`, and `--write` is refused outright. A non-null `verified_at`
means "an independent provider was asked and agreed", and there is no way to
earn that from a table we control.

    python scripts/confirm_mappings.py             # report only
    python scripts/confirm_mappings.py --write     # stamp verified_at
                                                   # (live_test_mode only)
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.config import get_settings
from app.db import session_scope
from app.integrations.razorpay.adapter import get_adapter


def main() -> int:
    write = "--write" in sys.argv
    s = get_settings()
    mode = s.resolved_razorpay_mode
    independent = mode != "mock"
    print(f"adapter: {mode}")
    if not independent:
        print("  The mock reads OUR payments table, so it agrees by "
              "construction.\n"
              "  This exercises the mapping layer; it confirms nothing.")
        if write:
            print("\n  Refusing --write. A non-null verified_at means an "
                  "independent\n  provider was asked and agreed. Set "
                  "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET\n  and run "
                  "`make spike` first.", file=sys.stderr)
            return 2

    confirmed = mismatched = missing = 0
    with session_scope() as session:
        adapter = get_adapter(session)
        rows = session.execute(text("""
            SELECT m.id, m.payment_id, m.external_payment_id, m.merchant_id,
                   m.verified_at, p.amount_minor
              FROM provider_mappings m
              JOIN payments p ON p.id = m.payment_id
             WHERE m.status = 'ACTIVE' AND m.external_payment_id IS NOT NULL
             ORDER BY m.payment_id
        """)).mappings().all()

        if not rows:
            print("no active externally-mapped payments to confirm")
            return 0

        for r in rows:
            try:
                ext = adapter.get_payment(r["external_payment_id"])
            except Exception as exc:
                # A failed read is not a disagreement. Left unverified, and
                # named, because "we could not check" and "we checked and it
                # was wrong" are different states and must not be merged.
                print(f"  UNREAD    {r['payment_id']} -> "
                      f"{r['external_payment_id']}: {type(exc).__name__}")
                missing += 1
                continue

            if ext is None:
                print(f"  MISSING   {r['payment_id']} -> "
                      f"{r['external_payment_id']}: provider has no such payment")
                missing += 1
            elif ext.amount_minor != r["amount_minor"]:
                print(f"  MISMATCH  {r['payment_id']} -> {r['external_payment_id']}: "
                      f"we hold {r['amount_minor']}, provider says {ext.amount_minor}")
                mismatched += 1
            else:
                confirmed += 1
                if write and independent:
                    session.execute(text(
                        "UPDATE provider_mappings SET verified_at = :now "
                        "WHERE id = :i"),
                        {"now": datetime.now(UTC), "i": r["id"]})

    verb = "confirmed" if independent else "round-tripped"
    print(f"\n{len(rows)} active mapping(s): {confirmed} {verb}, "
          f"{mismatched} mismatched, {missing} unreadable")
    if write and independent:
        print("verified_at stamped on the confirmed rows.")
    elif independent:
        print("Report only. Re-run with --write to stamp verified_at.")
    else:
        print("Nothing stamped: only an independent provider can earn "
              "verified_at.")

    # A mismatch is a mapping that would send money to the wrong payment.
    # Non-zero so this can gate something.
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
