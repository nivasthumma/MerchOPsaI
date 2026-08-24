"""Reconciliation sweep — settle actions left in UNKNOWN or PARTIAL.

Run by hand, or from cron:

    */5 * * * *  cd /path/to/merchantops-agent && .venv/bin/python scripts/reconcile.py

Never retries the action. Only re-reads external state, reconciling by
idempotency key, so it can never issue a second refund.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import session_scope
from app.verification.reconciler import escalated_actions, reconcile


def main() -> int:
    ap = argparse.ArgumentParser(description="Settle unsettled agent actions.")
    ap.add_argument("--min-age-seconds", type=int, default=30,
                    help="Ignore actions younger than this (default 30).")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="Escalate after this many attempts (default 5).")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = ap.parse_args()

    with session_scope() as s:
        rep = reconcile(s, min_age_seconds=args.min_age_seconds,
                        max_attempts=args.max_attempts, limit=args.limit)
        stuck = escalated_actions(s, max_attempts=args.max_attempts)

    if args.json:
        print(json.dumps({"report": rep.as_dict(), "escalated": stuck},
                         indent=2, default=str))
        return 0

    print("=" * 68)
    print("Reconciliation sweep")
    print("=" * 68)
    print(f"scanned             {rep.scanned}")
    print(f"settled             {rep.settled}")
    print(f"still unsettled     {rep.still_unsettled}")
    print(f"escalated           {rep.escalated}")

    if rep.details:
        print("\nPer action:")
        for d in rep.details:
            arrow = f"{d['from']} -> {d['to']}"
            flag = "  [ESCALATED]" if d.get("escalated") else ""
            err = f"  error={d['error']}" if d.get("error") else ""
            print(f"  {d['action_id']}  {arrow:22s} attempt {d.get('attempt')}{flag}{err}")

    if stuck:
        print("\n" + "!" * 68)
        print(f"{len(stuck)} action(s) need human investigation:")
        for a in stuck:
            print(f"  {a['id']}  payment={a['target_payment_id']} "
                  f"amount={a['amount_minor']} state={a['verification_state']} "
                  f"ref={a['external_reference']} attempts={a['verify_attempts']}")
        print("!" * 68)
        # Non-zero exit so a cron wrapper or monitor can alert on it.
        return 2

    if rep.scanned == 0:
        print("\nNothing to reconcile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
