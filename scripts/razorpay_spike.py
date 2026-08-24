"""Day-0 Razorpay Test Mode feasibility spike — CONTRACT §7.

Answers one question before any dependent work is scheduled:

    Can we obtain a captured Test Mode payment, retrieve it, refund it, and
    read the resulting refund state?

If yes  -> set RAZORPAY_MODE=live_test_mode and map real payment ids.
If no   -> the project runs on the mock adapter and SAYS SO. It never claims
           real Razorpay execution while mocked (CONTRACT §7, §54).

The spike writes docs/assessment/razorpay-spike.md with whatever it actually
observed, including failures. It does not assume the current API shape; it
reports what the API returned.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

REPORT = Path(__file__).resolve().parents[1] / "docs" / "assessment" / "razorpay-spike.md"
BASE = "https://api.razorpay.com/v1"


def main() -> int:
    s = get_settings()
    steps: list[dict] = []

    def step(name, ok, detail):
        steps.append({"step": name, "ok": ok, "detail": str(detail)[:600]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:150]}")

    if not (s.razorpay_key_id and s.razorpay_key_secret):
        step("credentials", False,
             "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in the environment.")
        write_report(steps, verdict="mock",
                     note="No credentials available in this environment.")
        print("\nVERDICT: mock adapter. Real Test Mode execution not demonstrated.")
        return 0

    import httpx
    client = httpx.Client(base_url=BASE, timeout=15.0,
                          auth=(s.razorpay_key_id, s.razorpay_key_secret))

    # ---- 1. credentials work at all ------------------------------------
    try:
        r = client.get("/payments", params={"count": 1})
        step("authenticate", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            write_report(steps, "mock", "Authentication failed.")
            return 1
    except Exception as e:                                   # noqa: BLE001
        step("authenticate", False, e)
        write_report(steps, "mock", "Could not reach the API.")
        return 1

    # ---- 2. is there a captured payment we can act on? -----------------
    items = r.json().get("items", [])
    captured = [p for p in items if p.get("status") == "captured"
                and int(p.get("amount", 0)) - int(p.get("amount_refunded", 0)) > 0]
    if not captured:
        r2 = client.get("/payments", params={"count": 100})
        captured = [p for p in r2.json().get("items", [])
                    if p.get("status") == "captured"
                    and int(p.get("amount", 0)) - int(p.get("amount_refunded", 0)) > 0]

    if not captured:
        step("find_captured_payment", False,
             "No refundable captured payment exists in this Test Mode account. "
             "Test Mode payments generally require completing Checkout with a test "
             "card; they cannot simply be POSTed. Create one manually, then re-run.")
        write_report(steps, "mock",
                     "Credentials work, but no captured payment is available to refund.")
        print("\nVERDICT: mock adapter until a captured test payment exists.")
        return 0

    payment = captured[0]
    pid = payment["id"]
    step("find_captured_payment", True, f"{pid} amount={payment['amount']}")

    # ---- 3. retrieve it -------------------------------------------------
    r3 = client.get(f"/payments/{pid}")
    step("get_payment", r3.status_code == 200, f"HTTP {r3.status_code}")

    # ---- 4. refund a token amount --------------------------------------
    amount = min(100, int(payment["amount"]) - int(payment.get("amount_refunded", 0)))
    key = f"spike-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    r4 = client.post(f"/payments/{pid}/refund",
                     json={"amount": amount, "speed": "normal",
                           "notes": {"idempotency_key": key}},
                     headers={"X-Payment-Idempotency": key})
    ok4 = r4.status_code in (200, 201)
    step("create_refund", ok4, f"HTTP {r4.status_code} {r4.text[:200]}")
    if not ok4:
        write_report(steps, "mock", "Refund creation failed against Test Mode.")
        return 1

    refund = r4.json()

    # ---- 5. read the resulting state (the verification predicate) -------
    r5 = client.get(f"/refunds/{refund['id']}")
    step("get_refund", r5.status_code == 200,
         f"HTTP {r5.status_code} status={r5.json().get('status') if r5.status_code == 200 else '-'}")

    r6 = client.get(f"/payments/{pid}")
    after = r6.json() if r6.status_code == 200 else {}
    moved = int(after.get("amount_refunded", 0)) > int(payment.get("amount_refunded", 0))
    step("payment_amount_refunded_moved", moved,
         f"before={payment.get('amount_refunded')} after={after.get('amount_refunded')} "
         f"refund_status={after.get('refund_status')}")

    verdict = "live_test_mode" if all(s_["ok"] for s_ in steps) else "mock"
    write_report(steps, verdict, "")
    print(f"\nVERDICT: {verdict}")
    return 0


def write_report(steps: list[dict], verdict: str, note: str) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Razorpay Test Mode feasibility spike",
        "",
        f"Run at: {datetime.now(timezone.utc).isoformat()}",
        f"**Verdict: `{verdict}`**",
        "",
    ]
    if note:
        lines += [note, ""]
    lines += ["| Step | Result | Detail |", "|---|---|---|"]
    for s_ in steps:
        detail = s_["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {s_['step']} | {'PASS' if s_['ok'] else 'FAIL'} | {detail} |")
    lines += [
        "",
        "## What this means",
        "",
        "- `live_test_mode` — the full obtain / retrieve / refund / verify cycle "
        "works and the demo executes real Test Mode refunds.",
        "- `mock` — the deterministic mock adapter is used. Policy, approval, "
        "idempotency and verification are **identical**; only the outbound HTTP "
        "call differs. The README and UI state this plainly. Per CONTRACT §7 and "
        "§54 the project never claims real Razorpay execution while mocked.",
        "",
        "```json",
        json.dumps(steps, indent=2),
        "```",
    ]
    REPORT.write_text("\n".join(lines))
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    raise SystemExit(main())
