"""Read-only investigation tools — CONTRACT §12, §17, §18.

Every query is scoped by merchant_id at the SQL level (CONTRACT §38). The
model supplies no merchant scope; it is injected from the authenticated
session by the executor.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from app.tools.contracts import Evidence, RiskClass, ToolResult, ToolSpec

# The dataset's fixed anchor. Real deployments would use now(); the fixed
# anchor is what makes evaluation reproducible (CONTRACT §30).
from scripts.seed_data import ANCHOR

PERIOD_DAYS = 7


def _fmt_inr(minor: int) -> str:
    return f"INR {minor / 100:,.2f}"


# --------------------------------------------------------------------------
# get_revenue_summary
# --------------------------------------------------------------------------
SPEC_REVENUE = ToolSpec(
    name="get_revenue_summary",
    description=(
        "Revenue for the current 7-day period and the preceding 7-day period, "
        "with the absolute and percentage change. Use this first when asked "
        "why revenue changed."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    required_permissions=["read:metrics"],
    risk_class=RiskClass.LOW,
)


def get_revenue_summary(session, merchant_id: str) -> ToolResult:
    cut = ANCHOR - timedelta(days=PERIOD_DAYS)
    prev_cut = ANCHOR - timedelta(days=PERIOD_DAYS * 2)
    sql = text("""
        SELECT
          COALESCE(SUM(CASE WHEN created_at >= :cut  AND status <> 'failed'
                            THEN amount_minor END), 0) AS cur_rev,
          COALESCE(SUM(CASE WHEN created_at >= :prev AND created_at < :cut AND status <> 'failed'
                            THEN amount_minor END), 0) AS prev_rev,
          COUNT(*) FILTER (WHERE created_at >= :cut  AND status <> 'failed') AS cur_n,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut AND status <> 'failed') AS prev_n
        FROM payments WHERE merchant_id = :m
    """)
    r = session.execute(sql, {"m": merchant_id, "cut": cut, "prev": prev_cut}).mappings().one()
    cur, prev = int(r["cur_rev"]), int(r["prev_rev"])
    delta = cur - prev
    pct = round(100.0 * delta / prev, 2) if prev else None
    data = {
        "current_period_revenue_minor": cur,
        "previous_period_revenue_minor": prev,
        "change_minor": delta,
        "change_pct": pct,
        "current_period_payments": int(r["cur_n"]),
        "previous_period_payments": int(r["prev_n"]),
        "current_period_start": cut.isoformat(),
        "period_days": PERIOD_DAYS,
        "currency": "INR",
    }
    ev = [
        Evidence(key="current_period_revenue", value=_fmt_inr(cur), source="payments"),
        Evidence(key="previous_period_revenue", value=_fmt_inr(prev), source="payments"),
        Evidence(key="change_pct", value=pct, source="payments"),
    ]
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# get_payment_metrics
# --------------------------------------------------------------------------
SPEC_PAYMENT_METRICS = ToolSpec(
    name="get_payment_metrics",
    description=(
        "Payment success/failure rates broken down by payment method, comparing "
        "the current 7-day period to the previous one. Optionally restrict to a "
        "single method to inspect its hourly failure distribution. Use this to "
        "find which payment method or time window is driving a change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": ["string", "null"],
                "enum": ["upi", "card", "netbanking", "wallet", None],
                "description": "Restrict to one method and return an hourly breakdown.",
            }
        },
        "required": ["method"],
    },
    required_permissions=["read:metrics"],
    risk_class=RiskClass.LOW,
)


def get_payment_metrics(session, merchant_id: str, method: str | None = None) -> ToolResult:
    cut = ANCHOR - timedelta(days=PERIOD_DAYS)
    prev_cut = ANCHOR - timedelta(days=PERIOD_DAYS * 2)
    params = {"m": merchant_id, "cut": cut, "prev": prev_cut}

    by_method = session.execute(text("""
        SELECT method,
          COUNT(*) FILTER (WHERE created_at >= :cut) AS cur_total,
          COUNT(*) FILTER (WHERE created_at >= :cut AND status <> 'failed') AS cur_ok,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut) AS prev_total,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut AND status <> 'failed') AS prev_ok
        FROM payments WHERE merchant_id = :m
        GROUP BY method ORDER BY method
    """), params).mappings().all()

    methods = []
    for row in by_method:
        cur_t, cur_o = int(row["cur_total"]), int(row["cur_ok"])
        pre_t, pre_o = int(row["prev_total"]), int(row["prev_ok"])
        cur_rate = round(100.0 * cur_o / cur_t, 1) if cur_t else None
        pre_rate = round(100.0 * pre_o / pre_t, 1) if pre_t else None
        methods.append({
            "method": row["method"],
            "current_success_rate_pct": cur_rate,
            "previous_success_rate_pct": pre_rate,
            "delta_pct_points": round(cur_rate - pre_rate, 1) if (cur_rate is not None and pre_rate is not None) else None,
            "current_total": cur_t,
            "current_failed": cur_t - cur_o,
        })

    data: dict = {"period_days": PERIOD_DAYS, "by_method": methods}
    ev = [Evidence(key=f"{m['method']}_success_change_pp",
                   value=m["delta_pct_points"], source="payments") for m in methods]

    if method:
        hourly = session.execute(text("""
            SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::int AS hr,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM payments
            WHERE merchant_id = :m AND method = :meth AND created_at >= :cut
            GROUP BY hr ORDER BY hr
        """), {**params, "meth": method}).mappings().all()
        rows = [{
            "hour": int(h["hr"]), "total": int(h["total"]), "failed": int(h["failed"]),
            "failure_rate_pct": round(100.0 * int(h["failed"]) / int(h["total"]), 1) if h["total"] else 0.0,
        } for h in hourly]
        data["hourly_breakdown"] = {"method": method, "rows": rows}

        top = sorted(rows, key=lambda r: r["failure_rate_pct"], reverse=True)[:3]
        data["hourly_breakdown"]["worst_hours"] = top
        ev.append(Evidence(key=f"{method}_worst_hours",
                           value=[f"{r['hour']:02d}:00 ({r['failure_rate_pct']}% failed)" for r in top],
                           source="payments"))

        err = session.execute(text("""
            SELECT error_reason, COUNT(*) AS n FROM payments
            WHERE merchant_id = :m AND method = :meth AND status = 'failed'
              AND created_at >= :cut AND error_reason IS NOT NULL
            GROUP BY error_reason ORDER BY n DESC LIMIT 5
        """), {**params, "meth": method}).mappings().all()
        data["hourly_breakdown"]["top_errors"] = [
            {"error_reason": e["error_reason"], "count": int(e["n"])} for e in err
        ]

    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# find_duplicate_payments
# --------------------------------------------------------------------------
SPEC_DUPLICATES = ToolSpec(
    name="find_duplicate_payments",
    description=(
        "Find likely duplicate payments: two or more captured payments on the "
        "same order, for the same customer and the same amount, close together "
        "in time. Returns a computed confidence per pair based on time "
        "separation. Use for duplicate-payment investigations."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "window_seconds": {
                "type": "integer", "minimum": 1, "maximum": 86400,
                "description": "Maximum separation between the two payments. Default 600.",
            }
        },
        "required": ["window_seconds"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def find_duplicate_payments(session, merchant_id: str, window_seconds: int = 600) -> ToolResult:
    rows = session.execute(text("""
        SELECT a.id AS a_id, b.id AS b_id, a.order_id, a.customer_id, a.amount_minor,
               a.method, a.created_at AS a_at, b.created_at AS b_at,
               EXTRACT(EPOCH FROM (b.created_at - a.created_at))::int AS gap_s,
               a.external_payment_id AS a_ext, b.external_payment_id AS b_ext,
               a.amount_refunded_minor AS a_refunded, b.amount_refunded_minor AS b_refunded
        FROM payments a
        JOIN payments b
          ON a.order_id = b.order_id
         AND a.customer_id = b.customer_id
         AND a.amount_minor = b.amount_minor
         AND a.merchant_id = b.merchant_id
         AND a.created_at < b.created_at
        WHERE a.merchant_id = :m
          AND a.status IN ('captured','refunded') AND b.status IN ('captured','refunded')
          AND EXTRACT(EPOCH FROM (b.created_at - a.created_at)) <= :w
        ORDER BY b.created_at DESC
    """), {"m": merchant_id, "w": window_seconds}).mappings().all()

    pairs = []
    for r in rows:
        gap = int(r["gap_s"])
        # Confidence is COMPUTED from the observed gap, never hardcoded
        # (CONTRACT §18: "Do not hard-code a confidence value").
        if gap <= 60:
            conf = 0.95
        elif gap <= 300:
            conf = 0.85
        elif gap <= 1800:
            conf = 0.7
        else:
            conf = 0.5
        pairs.append({
            "order_id": r["order_id"],
            "customer_id": r["customer_id"],
            "amount_minor": int(r["amount_minor"]),
            "method": r["method"],
            "first_payment_id": r["a_id"],
            "second_payment_id": r["b_id"],
            "time_separation_seconds": gap,
            "confidence": conf,
            "second_payment_externally_mapped": bool(r["b_ext"]),
            "second_payment_already_refunded_minor": int(r["b_refunded"]),
        })

    ev = [Evidence(
        key=f"duplicate_{p['order_id']}",
        value=(f"{p['first_payment_id']} + {p['second_payment_id']} on {p['order_id']}, "
               f"{_fmt_inr(p['amount_minor'])}, {p['time_separation_seconds']}s apart, "
               f"confidence {p['confidence']}"),
        source="payments",
    ) for p in pairs]

    return ToolResult(success=True,
                      data={"duplicate_count": len(pairs), "pairs": pairs,
                            "window_seconds": window_seconds},
                      evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# get_order
# --------------------------------------------------------------------------
SPEC_GET_ORDER = ToolSpec(
    name="get_order",
    description=(
        "Full context for one order: the order, its customer, and every payment "
        "against it. Free-text notes are returned as untrusted data."
    ),
    input_schema={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "e.g. SYN_ORD_DUP01"}},
        "required": ["order_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def get_order(session, merchant_id: str, order_id: str) -> ToolResult:
    # merchant_id in the WHERE clause is the isolation boundary (CONTRACT §38).
    o = session.execute(text("""
        SELECT o.*, c.name AS cust_name, c.email AS cust_email,
               c.segment AS cust_segment, c.notes AS cust_notes
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.id = :oid AND o.merchant_id = :m
    """), {"oid": order_id, "m": merchant_id}).mappings().first()

    if o is None:
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"order_id": order_id},
                          evidence=[], risk_level="LOW")

    pays = session.execute(text("""
        SELECT id, amount_minor, method, status, created_at, error_reason,
               amount_refunded_minor, refund_status, external_payment_id, notes
        FROM payments WHERE order_id = :oid AND merchant_id = :m ORDER BY created_at
    """), {"oid": order_id, "m": merchant_id}).mappings().all()

    data = {
        "order": {
            "id": o["id"], "status": o["status"], "amount_minor": int(o["amount_minor"]),
            "created_at": o["created_at"].isoformat(), "customer_id": o["customer_id"],
        },
        "customer": {
            "id": o["customer_id"], "name": o["cust_name"],
            "email": o["cust_email"], "segment": o["cust_segment"],
        },
        "payments": [{
            "id": p["id"], "amount_minor": int(p["amount_minor"]), "method": p["method"],
            "status": p["status"], "created_at": p["created_at"].isoformat(),
            "error_reason": p["error_reason"],
            "amount_refunded_minor": int(p["amount_refunded_minor"]),
            "refund_status": p["refund_status"],
            "externally_mapped": bool(p["external_payment_id"]),
        } for p in pays],
    }

    ev = [Evidence(key="order_amount", value=_fmt_inr(int(o["amount_minor"])), source="orders"),
          Evidence(key="payment_count", value=len(pays), source="payments")]

    # CONTRACT §36: free text is tagged untrusted at the boundary. The prompt
    # renderer wraps these; they are never interpolated bare.
    if o["notes"]:
        ev.append(Evidence(key="order_notes", value=o["notes"],
                           source="orders.notes", untrusted=True))
    if o["cust_notes"]:
        ev.append(Evidence(key="customer_notes", value=o["cust_notes"],
                           source="customers.notes", untrusted=True))
    for p in pays:
        if p["notes"]:
            ev.append(Evidence(key=f"payment_notes:{p['id']}", value=p["notes"],
                               source="payments.notes", untrusted=True))

    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# get_failure_breakdown — MerchantOps §18
# --------------------------------------------------------------------------
SPEC_FAILURE_BREAKDOWN = ToolSpec(
    name="get_failure_breakdown",
    description=(
        "Why payments failed: counts and value grouped by error reason, and by "
        "hour of day, for the current period. Optionally restrict to one payment "
        "method. Use this after get_payment_metrics has identified WHICH method "
        "is failing, to find out WHY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": ["string", "null"],
                "enum": ["upi", "card", "netbanking", "wallet", None],
                "description": "Restrict to one payment method.",
            }
        },
        "required": ["method"],
    },
    required_permissions=["read:metrics"],
    risk_class=RiskClass.LOW,
)


def get_failure_breakdown(session, merchant_id: str, method: str | None = None) -> ToolResult:
    cut = ANCHOR - timedelta(days=PERIOD_DAYS)
    params = {"m": merchant_id, "cut": cut}
    clause = "AND method = :meth" if method else ""
    if method:
        params["meth"] = method

    reasons = session.execute(text(f"""
        SELECT COALESCE(error_reason, 'UNSPECIFIED') AS reason,
               COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS value_minor
        FROM payments
        WHERE merchant_id = :m AND status = 'failed' AND created_at >= :cut {clause}
        GROUP BY reason ORDER BY n DESC
    """), params).mappings().all()

    hours = session.execute(text(f"""
        SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::int AS hr,
               COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS value_minor
        FROM payments
        WHERE merchant_id = :m AND status = 'failed' AND created_at >= :cut {clause}
        GROUP BY hr ORDER BY n DESC LIMIT 5
    """), params).mappings().all()

    total = sum(int(r["n"]) for r in reasons)
    total_value = sum(int(r["value_minor"]) for r in reasons)
    data = {
        "method": method, "period_days": PERIOD_DAYS,
        "total_failures": total, "total_failed_value_minor": total_value,
        "by_reason": [{
            "error_reason": r["reason"], "count": int(r["n"]),
            "value_minor": int(r["value_minor"]),
            "share_pct": round(100.0 * int(r["n"]) / total, 1) if total else 0.0,
        } for r in reasons],
        "worst_hours": [{
            "hour": int(h["hr"]), "failures": int(h["n"]),
            "value_minor": int(h["value_minor"]),
        } for h in hours],
    }
    ev = [Evidence(key="total_failures", value=total, source="payments"),
          Evidence(key="total_failed_value", value=_fmt_inr(total_value), source="payments")]
    ev += [Evidence(key=f"failures_{r['reason']}",
                    value=f"{int(r['n'])} ({_fmt_inr(int(r['value_minor']))})",
                    source="payments") for r in reasons[:5]]
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# get_payment — MerchantOps §18
# --------------------------------------------------------------------------
SPEC_GET_PAYMENT = ToolSpec(
    name="get_payment",
    description=(
        "One payment in full: amount, method, status, failure reason, refund "
        "state, and whether it is mapped to the provider. Free-text notes are "
        "returned as untrusted data."
    ),
    input_schema={
        "type": "object",
        "properties": {"payment_id": {"type": "string", "description": "e.g. SYN_PAY_0002"}},
        "required": ["payment_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def get_payment(session, merchant_id: str, payment_id: str) -> ToolResult:
    # merchant_id in the WHERE clause is the isolation boundary (§54).
    r = session.execute(text("""
        SELECT p.*, o.status AS order_status
        FROM payments p LEFT JOIN orders o ON o.id = p.order_id
        WHERE p.id = :p AND p.merchant_id = :m
    """), {"p": payment_id, "m": merchant_id}).mappings().first()
    if r is None:
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"payment_id": payment_id}, risk_level="LOW")

    actions = session.execute(text("""
        SELECT id, action_type, status, verification_state, amount_minor
        FROM agent_actions WHERE target_payment_id = :p AND merchant_id = :m
        ORDER BY created_at
    """), {"p": payment_id, "m": merchant_id}).mappings().all()

    data = {
        "id": r["id"], "order_id": r["order_id"], "customer_id": r["customer_id"],
        "amount_minor": int(r["amount_minor"]), "method": r["method"],
        "status": r["status"], "error_reason": r["error_reason"],
        "amount_refunded_minor": int(r["amount_refunded_minor"]),
        "refundable_balance_minor": int(r["amount_minor"]) - int(r["amount_refunded_minor"]),
        "refund_status": r["refund_status"],
        "created_at": r["created_at"].isoformat(),
        "order_status": r["order_status"],
        "externally_mapped": bool(r["external_payment_id"]),
        # Prior actions matter: acting on a payment whose last attempt is
        # unsettled is graded CRITICAL, and the model should be able to see why.
        "prior_actions": [{
            "id": a["id"], "type": a["action_type"], "status": a["status"],
            "verification_state": a["verification_state"],
            "amount_minor": int(a["amount_minor"]),
        } for a in actions],
    }
    ev = [
        Evidence(key="payment_amount", value=_fmt_inr(int(r["amount_minor"])), source="payments"),
        Evidence(key="payment_status", value=r["status"], source="payments"),
        Evidence(key="refundable_balance",
                 value=_fmt_inr(data["refundable_balance_minor"]), source="payments"),
    ]
    if r["error_reason"]:
        ev.append(Evidence(key="error_reason", value=r["error_reason"], source="payments"))
    # §39: free text is tagged untrusted at the boundary, never interpolated bare.
    if r["notes"]:
        ev.append(Evidence(key="payment_notes", value=r["notes"],
                           source="payments.notes", untrusted=True))
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")


# --------------------------------------------------------------------------
# get_customer — MerchantOps §18
# --------------------------------------------------------------------------
SPEC_GET_CUSTOMER = ToolSpec(
    name="get_customer",
    description=(
        "One customer's profile and payment history summary, including whether "
        "they have opted out of contact. Free-text notes are returned as "
        "untrusted data."
    ),
    input_schema={
        "type": "object",
        "properties": {"customer_id": {"type": "string", "description": "e.g. SYN_CUS_A0012"}},
        "required": ["customer_id"],
    },
    required_permissions=["read:orders"],
    risk_class=RiskClass.LOW,
)


def get_customer(session, merchant_id: str, customer_id: str) -> ToolResult:
    c = session.execute(text("""
        SELECT id, name, email, segment, contact_opted_out, notes
        FROM customers WHERE id = :c AND merchant_id = :m
    """), {"c": customer_id, "m": merchant_id}).mappings().first()
    if c is None:
        return ToolResult(success=False, error_code="NOT_FOUND",
                          data={"customer_id": customer_id}, risk_level="LOW")

    agg = session.execute(text("""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               COALESCE(SUM(amount_minor) FILTER (WHERE status <> 'failed'), 0) AS paid_minor,
               COALESCE(SUM(amount_refunded_minor), 0) AS refunded_minor
        FROM payments WHERE customer_id = :c AND merchant_id = :m
    """), {"c": customer_id, "m": merchant_id}).mappings().one()

    data = {
        "id": c["id"], "name": c["name"], "email": c["email"], "segment": c["segment"],
        # §28 makes this a stopping condition, so the model must be able to see
        # it before it recommends contacting anyone.
        "contact_opted_out": bool(c["contact_opted_out"]),
        "payments": int(agg["n"]), "failed_payments": int(agg["failed"]),
        "lifetime_paid_minor": int(agg["paid_minor"]),
        "lifetime_refunded_minor": int(agg["refunded_minor"]),
    }
    ev = [
        Evidence(key="customer_segment", value=c["segment"], source="customers"),
        Evidence(key="contact_opted_out", value=bool(c["contact_opted_out"]),
                 source="customers"),
        Evidence(key="lifetime_paid", value=_fmt_inr(int(agg["paid_minor"])), source="payments"),
        Evidence(key="failed_payments", value=int(agg["failed"]), source="payments"),
    ]
    if c["notes"]:
        ev.append(Evidence(key="customer_notes", value=c["notes"],
                           source="customers.notes", untrusted=True))
    return ToolResult(success=True, data=data, evidence=ev, risk_level="LOW")
