"""Send the notifications that nothing raises an event for.

    PYTHONPATH=. python scripts/notify_sweep.py

Run it on a cadence. An approval's default life is fifteen minutes
(`approval_ttl_seconds`) and the chase fires five minutes before the end
(`notify_approval_warning_seconds`), so a sweep running less often than every
few minutes will reliably deliver the warning after the window it was warning
about has closed. Every two minutes is a reasonable default; over-running costs
queries and sends nothing twice, because `dedupe_key` is UNIQUE.

This drains the event spine first. The two are separate mechanisms -- consumers
handle what happened, the sweep handles what did not -- but a deployment with no
worker needs both called, and calling one without the other produces exactly
half a notification system.

Exits non-zero when something was recorded and could not be delivered, so a cron
that reports failures reports this one.
"""
from __future__ import annotations

import json
import sys

from app.db import session_scope
from app.events.bus import drain
from app.notify import register, sweep
from app.notify.service import retry_pending


def main() -> int:
    register()

    with session_scope() as s:
        drained = drain(s, limit=1000)

    with session_scope() as s:
        swept = sweep(s)

    # Anything an earlier run recorded and could not get out. Retried here
    # rather than in `sweep` so that a channel outage recovers on the next
    # cadence without needing a separate operator action.
    with session_scope() as s:
        retried = retry_pending(s).as_dict()

    report = {"drained": drained, "swept": swept, "retried": retried}
    print(json.dumps(report, indent=2))

    undelivered = (swept["approvals"]["failed"] + swept["escalated"]["failed"]
                   + retried["failed"])
    if undelivered:
        print(f"\n{undelivered} notification(s) recorded and NOT delivered. "
              f"GET /notifications shows them with the channel error.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
