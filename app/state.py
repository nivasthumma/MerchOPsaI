"""The merchant digital twin — MerchantOps v2 §14, ADR-0040.

§14 asks for "a continuously updated representation of merchant operational
health", with six branches: Financial, Payments, Customers, Incidents, Recovery,
Operational Health.

Almost all of it already existed, and none of it existed *together*: revenue in
`get_revenue_summary`, revenue-at-risk in `RecoveryLedger`, method health in
`get_payment_metrics`, incidents and recovery in `ledger.dashboard`, operational
health in `app.metrics`. So this module assembles rather than computes, and
calls the existing owners rather than reimplementing their arithmetic. A figure
recomputed beside its owner is how two answers to one question get created.

## It is a projection, not a table

There is no `merchant_state` row. The figures derive from rows that change
underneath them, and a cached count is a count that disagrees with its rows the
first time a candidate moves — the argument `app/recovery/campaign.py` already
makes for the campaign card.

§14's "continuously updated" is satisfied by *being* the rows rather than by a
refresh loop chasing them. A projection is current by construction; a stored
twin is current only as often as somebody remembered to invalidate it.
`build_state` is the seam a cache would go behind if a measurement ever demanded
one, and none does at this size.

## Unmeasurable branches say so

§14 lists `Payments → Latency`. `agent_actions` records `provider_latency_ms`,
but that is *our* call to Razorpay; §14 means how long a customer's payment took
at the rail, and `payments` has a `created_at` with nothing to subtract from it.
Recording it is a change to what is collected, not to what is reported.

So the branch reports `measured=False` with a reason instead of a zero. A figure
computed from nothing is worse than a blank, because the blank prompts the
question and the number closes it — `app/metrics.py` set this precedent and
ADR-0034 repeated it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from scripts.seed_data import ANCHOR

# The comparison window every branch uses, so the twin cannot report a revenue
# figure for one period beside a success rate for another.
PERIOD_DAYS = 7


@dataclass
class Branch:
    """One limb of §14's tree, and whether it could be measured at all."""
    values: dict = field(default_factory=dict)
    measured: bool = True
    unmeasured_reason: str = ""

    def as_dict(self) -> dict:
        out = dict(self.values)
        if not self.measured:
            out["measured"] = False
            out["reason"] = self.unmeasured_reason
        return out


@dataclass
class MerchantState:
    """§14's MerchantState. Assembled per read; nothing here is stored."""
    merchant_id: str
    as_of: str
    period_days: int
    financial: Branch
    payments: Branch
    customers: Branch
    incidents: Branch
    recovery: Branch
    operational_health: Branch

    def as_dict(self) -> dict:
        return {
            "merchant_id": self.merchant_id,
            # A dashboard figure with no as-of is a figure somebody quotes an
            # hour later. Computed-per-read means "as of this request", and
            # saying so is cheaper than being asked.
            "as_of": self.as_of,
            "period_days": self.period_days,
            "financial": self.financial.as_dict(),
            "payments": self.payments.as_dict(),
            "customers": self.customers.as_dict(),
            "incidents": self.incidents.as_dict(),
            "recovery": self.recovery.as_dict(),
            "operational_health": self.operational_health.as_dict(),
        }

    # ------------------------------------------------------------------
    def for_incident(self, incident) -> dict:
        """The slice of the twin that bears on one incident — §14, §26.

        §14 ends "The AI receives relevant portions of it" and §26 says not to
        send the whole database. Handing the model the full state would be a
        context bill rather than a context strategy, and most of it — other
        methods' health, the merchant's whole ledger — cannot inform this
        incident.

        Facts only, and stated as facts. `build_investigation_request` already
        establishes that the model is not asked to re-derive figures the
        calculation engine owns (§22) and is not free to contradict them.
        """
        method = (incident.signals or {}).get("method")
        methods = self.payments.values.get("by_method", [])
        relevant = [m for m in methods if method and m["method"] == method]

        return {
            "as_of": self.as_of,
            "period_days": self.period_days,
            # The merchant's exposure, for scale. Not the full ledger.
            "revenue_at_risk_minor": self.financial.values.get("revenue_at_risk_minor"),
            "current_period_revenue_minor": self.financial.values.get(
                "current_period_revenue_minor"),
            # This incident's method, or every method when the incident names
            # none — a duplicate payment is not about a rail.
            "method_health": relevant or methods,
            "open_incidents": self.incidents.values.get("open"),
            "customers_affected": self.customers.values.get("affected"),
            "recovery_candidates": self.customers.values.get("recovery_candidates"),
        }


# --------------------------------------------------------------------------
def build_state(session, merchant_id: str) -> MerchantState:
    """Assemble §14's tree for one merchant.

    Every branch calls the module that already owns its figures. The one new
    computation is GMV, which §14 names and nothing produced.
    """
    from app.metrics import operational_metrics
    from app.recovery.ledger import build_ledger, dashboard

    cut = ANCHOR - timedelta(days=PERIOD_DAYS)
    prev_cut = ANCHOR - timedelta(days=PERIOD_DAYS * 2)
    params = {"m": merchant_id, "cut": cut, "prev": prev_cut}

    ledger = build_ledger(session, merchant_id)
    board = dashboard(session, merchant_id)

    return MerchantState(
        merchant_id=merchant_id,
        as_of=datetime.now(timezone.utc).isoformat(),
        period_days=PERIOD_DAYS,
        financial=_financial(session, params, ledger),
        payments=_payments(session, params),
        customers=_customers(session, merchant_id),
        incidents=Branch(board["incidents"]),
        recovery=Branch(board["recovery"]),
        operational_health=Branch({
            "metrics": operational_metrics(session, merchant_id),
            "agent_activity": board["agent_activity"],
        }),
    )


def _financial(session, params: dict, ledger) -> Branch:
    """§14's Financial branch: revenue, GMV, refunds, revenue at risk.

    GMV and revenue are deliberately separate. GMV is what customers *tried* to
    spend and revenue is what was *captured*; the ratio between them is the
    conversion story §14 puts them side by side to tell, and reporting GMV as a
    bigger revenue number would tell the opposite one.
    """
    row = session.execute(text("""
        SELECT
          COALESCE(SUM(amount_minor) FILTER (
              WHERE created_at >= :cut AND status <> 'failed'), 0)   AS revenue,
          COALESCE(SUM(amount_minor) FILTER (
              WHERE created_at >= :prev AND created_at < :cut
                AND status <> 'failed'), 0)                          AS prev_revenue,
          COALESCE(SUM(amount_minor) FILTER (
              WHERE created_at >= :cut), 0)                          AS gmv,
          COALESCE(SUM(amount_refunded_minor) FILTER (
              WHERE created_at >= :cut), 0)                          AS refunded
        FROM payments WHERE merchant_id = :m
    """), params).mappings().one()

    revenue, gmv = int(row["revenue"]), int(row["gmv"])
    prev = int(row["prev_revenue"])
    return Branch({
        "current_period_revenue_minor": revenue,
        "previous_period_revenue_minor": prev,
        "revenue_change_pct": (round(100.0 * (revenue - prev) / prev, 1)
                               if prev else None),
        # Attempted value, captured or not.
        "gmv_minor": gmv,
        "gmv_definition": ("Value of all payment attempts in the period, "
                           "captured or not. Revenue is the captured subset."),
        "capture_rate_pct": round(100.0 * revenue / gmv, 1) if gmv else None,
        "refunded_minor": int(row["refunded"]),
        # §22 owns this; it is read from the ledger, never recomputed here.
        "revenue_at_risk_minor": ledger.at_risk_minor,
    })


def _payments(session, params: dict) -> Branch:
    """§14's Payments branch: success rate, failure rate, method health.

    Latency is the branch §14 names that this system cannot fill — see the
    module docstring. Reported as unmeasured rather than as zero.
    """
    rows = session.execute(text("""
        SELECT method,
          COUNT(*) FILTER (WHERE created_at >= :cut)                       AS cur_total,
          COUNT(*) FILTER (WHERE created_at >= :cut AND status <> 'failed') AS cur_ok,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut) AS prev_total,
          COUNT(*) FILTER (WHERE created_at >= :prev AND created_at < :cut
                             AND status <> 'failed')                        AS prev_ok
        FROM payments WHERE merchant_id = :m
        GROUP BY method ORDER BY method
    """), params).mappings().all()

    methods, total, ok = [], 0, 0
    for r in rows:
        cur_t, cur_o = int(r["cur_total"]), int(r["cur_ok"])
        pre_t, pre_o = int(r["prev_total"]), int(r["prev_ok"])
        total += cur_t
        ok += cur_o
        cur_rate = round(100.0 * cur_o / cur_t, 1) if cur_t else None
        pre_rate = round(100.0 * pre_o / pre_t, 1) if pre_t else None
        methods.append({
            "method": r["method"],
            "success_rate_pct": cur_rate,
            "previous_success_rate_pct": pre_rate,
            "delta_pct_points": (round(cur_rate - pre_rate, 1)
                                 if cur_rate is not None and pre_rate is not None
                                 else None),
            "attempts": cur_t,
            "failed": cur_t - cur_o,
        })

    return Branch({
        "success_rate_pct": round(100.0 * ok / total, 1) if total else None,
        "failure_rate_pct": round(100.0 * (total - ok) / total, 1) if total else None,
        "attempts": total,
        "failed": total - ok,
        "by_method": methods,
        # §14 names it; nothing records it. Marked rather than zeroed.
        "latency": {
            "measured": False,
            "reason": ("Payment latency at the rail is not collected. "
                       "`agent_actions.provider_latency_ms` measures this "
                       "system's own provider calls, which is a different "
                       "quantity, and `payments` carries no elapsed time."),
        },
    })


def _customers(session, merchant_id: str) -> Branch:
    """§14's Customers branch: active, affected, recovery candidates.

    "Affected" is the count of distinct customers with a candidate against an
    open incident — the only place the system actually knows the answer, and the
    same source the evidence graph's AFFECTS edge draws from.
    """
    row = session.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM customers WHERE merchant_id = :m)      AS active,
          (SELECT COUNT(DISTINCT rc.customer_id)
             FROM recovery_candidates rc
             JOIN incidents i ON i.id = rc.incident_id
            WHERE rc.merchant_id = :m
              AND i.status NOT IN ('RESOLVED','CLOSED'))               AS affected,
          (SELECT COUNT(*) FROM recovery_candidates
            WHERE merchant_id = :m AND status = 'ELIGIBLE')            AS candidates,
          (SELECT COUNT(*) FROM customers
            WHERE merchant_id = :m AND contact_opted_out)              AS opted_out
    """), {"m": merchant_id}).mappings().one()

    return Branch({
        "active": int(row["active"]),
        "affected": int(row["affected"]),
        "recovery_candidates": int(row["candidates"]),
        # §28's stopping rule is about these people; a console that cannot see
        # how many there are cannot see why a campaign is smaller than expected.
        "opted_out": int(row["opted_out"]),
    })
