"""Read models for the operations console — plan P0-03, P0-05, P1-06.

Three aggregations that existed only as things a browser could have assembled
from five other endpoints, and therefore did not exist:

    action_center()   the financial operations queue      (P0-03, P0-04)
    command_center()  "what needs my attention?"          (P0-05, P1-03)
    search()          one box, every identifier           (P1-06)

## Why they are server-side

A client that fetches `/approvals`, `/actions/escalated`, `/incidents` and
`/recovery/ledger` and adds them up is a client that has opinions about money.
Four requests also means four different instants: the approval count and the
UNKNOWN count would be from different moments, and the difference shows up
exactly when the numbers are moving, which is exactly when someone is watching.

So each of these is one query set inside one transaction, and the browser
renders what it is handed. That is the same rule the ops strip already follows
(`/metrics`), applied to the screens the plan asks for.

## Merchant scope

Every query here takes `merchant_id` as a bound parameter and there is no path
that omits it. The value comes from the authenticated principal at the call
site in `app/api/main.py`, never from a query string — MerchantOps §44.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from app.verification.schedule import MAX_ATTEMPTS

# --------------------------------------------------------------------------
# Action Center — P0-03
# --------------------------------------------------------------------------
# The five sections the plan names, in the order it names them. Order matters:
# the page is read top-down by someone under time pressure, and "awaiting
# approval" is the section with a human in the critical path.
SECTIONS = ("awaiting_approval", "executing", "unknown", "escalated",
            "recently_completed", "failed")

# Everything one row needs to be judged without opening it: amount,
# customer/payment, incident/task, reason, risk, policy, approval, provider
# reference, verification state, attempts, age, next action (plan P0-03).
#
# One SELECT list, five sections. A per-section projection is how two sections
# come to disagree about what `amount_minor` means.
_ACTION_COLUMNS = """
    a.id, a.task_id, a.merchant_id, a.action_type, a.status,
    a.target_payment_id, a.external_payment_id, a.external_reference,
    a.amount_minor, a.verification_state, a.verify_attempts,
    a.escalated, a.escalated_at, a.last_verified_at, a.next_verify_at,
    a.approval_id, a.recovery_candidate_id,
    a.created_at, a.updated_at,
    a.provider_latency_ms, a.verification_latency_ms,
    p.customer_id, p.method AS payment_method,
    m.provider, m.environment,
    t.incident_id, t.user_id AS owner, t.request AS task_request,
    t.status AS task_status,
    ap.decision AS approval_decision, ap.risk_level, ap.expires_at,
    ap.required_signatures
"""

_ACTION_JOINS = """
    FROM agent_actions a
    LEFT JOIN payments p ON p.id = a.target_payment_id
    LEFT JOIN agent_tasks t ON t.id = a.task_id
    LEFT JOIN approvals ap ON ap.id = a.approval_id
    LEFT JOIN provider_mappings m
      ON m.payment_id = a.target_payment_id AND m.status = 'ACTIVE'
"""


def _rows(session, where: str, params: dict, order: str, limit: int) -> list[dict]:
    sql = (f"SELECT {_ACTION_COLUMNS} {_ACTION_JOINS} "
           f"WHERE a.merchant_id = :m AND ({where}) "
           f"ORDER BY {order} LIMIT :lim")
    return [dict(r) for r in session.execute(
        text(sql), {**params, "lim": limit}).mappings().all()]


def _count(session, where: str, params: dict) -> int:
    """How many rows the same predicate matches, ignoring the page size.

    A separate query rather than `len(rows)`, and that distinction is the whole
    point of this function existing. Every section is capped at `limit`, so
    `len(rows)` is the size of the PAGE. Reporting it as the count meant that
    with sixty unresolved actions the Command Center said sixty (it counts with
    SQL) and the Action Center said fifty — two screens disagreeing about how
    much unresolved financial work exists, with the smaller number on the page
    an operator acts from.

    `where` and `order` are composed from string literals written in this
    module; every caller-supplied value is bound. Nothing that reaches these
    f-strings came from a request.
    """
    sql = (f"SELECT COUNT(*) {_ACTION_JOINS} "
           f"WHERE a.merchant_id = :m AND ({where})")
    return int(session.execute(text(sql), params).scalar() or 0)


def action_center(session, merchant_id: str, *, limit: int = 50) -> dict:
    """The financial operations queue, in five sections.

    An action appears in exactly one section. The classification is by state,
    evaluated in the order below, so the sections partition the queue rather
    than overlapping — a row in two places is a row somebody will action twice.

      escalated           the system gave up; a human owns it
      unknown             unsettled, still being worked automatically
      awaiting_approval   the money has not moved and will not until a human says
      executing           claimed or submitted, outcome not yet read
      recently_completed  settled, either way

    `escalated` is tested before `unknown` deliberately: an escalated action is
    still UNKNOWN, and listing it under "we are working on it" is the precise
    misreport this section exists to prevent.
    """
    params = {"m": merchant_id}

    # One predicate per section, named once. The page and the count are two
    # queries over the same clause, and two copies of a clause is how a queue
    # comes to show rows its own count says are not there.
    escalated_where = ("a.escalated = true AND (a.verification_state IS NULL "
                       "OR a.verification_state IN ('UNKNOWN', 'PARTIAL'))")
    # SUBMITTED joins PENDING: the provider reference is committed before
    # verification reads the provider, so a request that dies during those
    # reads leaves a submitted action with no outcome. With PENDING alone it
    # fell out of every section once it aged past `executing` -- a hidden
    # UNKNOWN, which this queue exists to make impossible.
    unknown_where = (
        "a.escalated = false AND (a.verification_state IN ('UNKNOWN', 'PARTIAL') "
        "OR (a.verification_state IS NULL AND a.status IN ('PENDING', 'SUBMITTED') "
        "    AND a.created_at < now() - interval '2 minutes'))")
    executing_where = (
        "a.escalated = false AND a.verification_state IS NULL "
        "AND a.status IN ('PENDING', 'SUBMITTED') "
        "AND a.created_at >= now() - interval '2 minutes'")
    # Completed and failed are separate sections: "settled" is true of both,
    # and an operator scanning for what went wrong should not have to read
    # every success to find it.
    completed_where = "a.verification_state = 'SUCCESS'"
    failed_where = "a.verification_state = 'FAILED'"

    # Approvals that no action has been claimed for yet. These are the rows the
    # plan cares most about — the human is the gate and the money has not moved
    # — and they live in `approvals`, not `agent_actions`, because no action row
    # exists until the approval clears.
    pending_approvals = [dict(r) for r in session.execute(text("""
        SELECT ap.id AS approval_id, ap.task_id, ap.action_type, ap.action_payload,
               ap.risk_level, ap.decision, ap.expires_at, ap.required_signatures,
               ap.created_at, ap.evidence,
               t.incident_id, t.user_id AS owner, t.request AS task_request,
               (SELECT COUNT(*) FROM approval_signatures s
                 WHERE s.approval_id = ap.id) AS signatures,
               -- Expiry is computed by the database against the database's own
               -- clock. A browser deciding an approval has expired, from a
               -- timestamp and its own clock, is a browser that can be wrong in
               -- the direction that matters.
               (ap.expires_at <= now()) AS expired
          FROM approvals ap
          LEFT JOIN agent_tasks t ON t.id = ap.task_id
         WHERE ap.merchant_id = :m AND ap.decision = 'PENDING'
         ORDER BY ap.created_at
         LIMIT :lim
    """), {**params, "lim": limit}).mappings().all()]

    # Escalated AND still unresolved. `escalated` is a historical fact that
    # stays true once set -- a human was handed this -- but the section is a
    # work queue, and an action a later re-verification settled is finished
    # work. Without the second clause such an action appears here *and* under
    # "recently completed", and the sections stop partitioning: an operator
    # picks up something already done, in the one place where doing something
    # twice moves money twice.
    escalated = _rows(session, escalated_where, params,
                      "a.escalated_at NULLS LAST, a.created_at", limit)
    # Soonest next check first, so the queue reads as a schedule.
    unknown = _rows(session, unknown_where, params,
                    "a.next_verify_at NULLS FIRST, a.created_at", limit)
    executing = _rows(session, executing_where, params, "a.created_at DESC", limit)
    completed = _rows(session, completed_where, params, "a.updated_at DESC", limit)
    failed = _rows(session, failed_where, params, "a.updated_at DESC", limit)

    # True totals, not page lengths. See `_count`.
    counts = {
        "awaiting_approval": int(session.execute(text("""
            SELECT COUNT(*) FROM approvals
             WHERE merchant_id = :m AND decision = 'PENDING'
        """), params).scalar() or 0),
        "executing": _count(session, executing_where, params),
        "unknown": _count(session, unknown_where, params),
        "escalated": _count(session, escalated_where, params),
        "recently_completed": _count(session, completed_where, params),
        "failed": _count(session, failed_where, params),
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "merchant_id": merchant_id,
        "awaiting_approval": pending_approvals,
        "executing": executing,
        "unknown": unknown,
        "escalated": escalated,
        "recently_completed": completed,
        "failed": failed,
        "counts": counts,
        # How many rows each section actually returned. A client showing
        # `counts` beside a shorter list is showing a number it cannot
        # substantiate, so it is told both and can say "50 of 60" rather than
        # dropping ten silently.
        "shown": {
            "awaiting_approval": len(pending_approvals),
            "executing": len(executing),
            "unknown": len(unknown),
            "escalated": len(escalated),
            "recently_completed": len(completed),
            "failed": len(failed),
        },
        "limit": limit,
        # Published so the UI can render "next check in 4m" and "gives up after
        # 5 attempts" from the system's own rule rather than a copy of it.
        "reconciliation_policy": {
            "max_attempts": MAX_ATTEMPTS,
            "on_exhaustion": "escalate",
        },
        # The section list, in order, so a client cannot invent a sixth or drop
        # one by forgetting to render it.
        "sections": list(SECTIONS),
    }


def incident_actions(session, merchant_id: str, incident_id: str) -> list[dict]:
    """Every financial action that came out of one incident — plan P0-07.

    The same projection the Action Center uses, filtered to one incident, so the
    incident workspace and the queue cannot disagree about the state of an
    action. Two projections of one row is how a page comes to say CONFIRMED
    while another says UNKNOWN.

    Ordered oldest first: on this page the actions are read as a sequence of
    what was done, not as a work queue.
    """
    return _rows(session, "t.incident_id = :inc",
                 {"m": merchant_id, "inc": incident_id},
                 "a.created_at", 200)


# --------------------------------------------------------------------------
# Command Center — P0-05
# --------------------------------------------------------------------------
def command_center(session, merchant_id: str) -> dict:
    """What needs my attention, as one read.

    Composed from the existing builders rather than re-querying what they
    already own: `build_ledger` is the authority on the recovery funnel and
    `dashboard` on incident and agent counts. A second implementation of "at
    risk" here would be a second number that disagrees under load.

    The additions are the ones the plan names that nothing computed yet: the
    live attention counts, the funnel as an ordered structure rather than six
    loose figures, and the provider/agent posture.
    """
    from app.recovery.ledger import build_ledger

    led = build_ledger(session, merchant_id).as_dict()

    attention = session.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM approvals
            WHERE merchant_id = :m AND decision = 'PENDING'
              AND expires_at > now())                              AS approvals_pending,
          (SELECT COUNT(*) FROM approvals
            WHERE merchant_id = :m AND decision = 'PENDING'
              AND expires_at <= now())                             AS approvals_expired,
          (SELECT COUNT(*) FROM agent_actions
            WHERE merchant_id = :m AND escalated = false
              AND verification_state IN ('UNKNOWN', 'PARTIAL'))    AS unknown_actions,
          (SELECT COUNT(*) FROM agent_actions
            WHERE merchant_id = :m AND escalated = true)           AS escalated_actions,
          (SELECT COUNT(*) FROM incidents
            WHERE merchant_id = :m
              AND status NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED')) AS open_incidents,
          (SELECT COUNT(*) FROM incidents
            WHERE merchant_id = :m AND severity = 'CRITICAL'
              AND status NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED')) AS critical_incidents,
          (SELECT COUNT(*) FROM agent_tasks
            WHERE merchant_id = :m AND status = 'RUNNING')         AS running_tasks
    """), {"m": merchant_id}).mappings().one()

    # The funnel, ordered and labelled. P1-03's rule is that at-risk must never
    # read as recovered, and the surest way to keep that true is to hand the UI
    # an ordered list of named stages instead of six numbers it arranges itself.
    funnel = [
        {"stage": "AT_RISK", "label": "At risk", "amount_minor": led["at_risk_minor"]},
        {"stage": "RECOVERABLE", "label": "Recoverable",
         "amount_minor": led["recoverable_minor"]},
        {"stage": "ATTEMPTED", "label": "Attempted",
         "amount_minor": led["attempted_minor"]},
        {"stage": "RECOVERED", "label": "Recovered",
         "amount_minor": led["recovered_minor"]},
    ]

    recent = [dict(r) for r in session.execute(text("""
        SELECT event_type, correlation_id, task_id, incident_id, created_at, payload
          FROM audit_logs
         WHERE merchant_id = :m
         ORDER BY created_at DESC
         LIMIT 25
    """), {"m": merchant_id}).mappings().all()]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "merchant_id": merchant_id,
        "revenue": {
            "at_risk_minor": led["at_risk_minor"],
            "recoverable_minor": led["recoverable_minor"],
            "attempted_minor": led["attempted_minor"],
            "recovered_minor": led["recovered_minor"],
            "failed_minor": led["failed_minor"],
            "unknown_minor": led["unknown_minor"],
            "outstanding_minor": led["outstanding_minor"],
            "invariants_broken": led["invariants_broken"],
            "recovered_captured_minor": led["recovered_captured_minor"],
            "recovered_refunded_minor": led["recovered_refunded_minor"],
        },
        "funnel": funnel,
        "attention": {k: int(v) for k, v in dict(attention).items()},
        "by_incident": led["by_incident"],
        "by_method": led["by_method"],
        "activity": recent,
        "agent": _agent_posture(session, merchant_id),
        "provider": _provider_posture(session, merchant_id),
    }


def _agent_posture(session, merchant_id: str) -> dict:
    """Configured reasoning, and how this merchant's runs were really produced."""
    from app.config import get_settings
    from app.llm.deterministic import DeterministicProvider

    s = get_settings()
    provider = s.resolved_llm_provider
    by_mode = {(r[0] or "UNRECORDED"): int(r[1]) for r in session.execute(text("""
        SELECT ai_mode, COUNT(*) FROM agent_tasks
         WHERE merchant_id = :m AND is_replay = false
         GROUP BY ai_mode
    """), {"m": merchant_id}).all()}
    return {"provider": provider,
            "model": s.llm_model if provider == "anthropic" else DeterministicProvider.model,
            "fallback_enabled": s.llm_fallback_enabled,
            "runs_by_mode": by_mode}


def _provider_posture(session, merchant_id: str) -> dict:
    """Where actions go, and whether provider deliveries are being processed."""
    from app.config import get_settings

    s = get_settings()
    mode = s.resolved_razorpay_mode
    hooks = session.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status = 'RECEIVED' AND signature_valid) AS pending,
               COUNT(*) FILTER (WHERE status = 'FAILED')                       AS dead,
               MAX(received_at)                                                AS last_at
          FROM webhook_events WHERE merchant_id = :m
    """), {"m": merchant_id}).mappings().one()
    return {"adapter_mode": mode, "live": mode == "live_test_mode",
            "webhook_signature_verification": s.webhook_verification_enabled,
            "webhooks_pending": int(hooks["pending"] or 0),
            "webhooks_dead_lettered": int(hooks["dead"] or 0),
            "last_webhook_at": hooks["last_at"].isoformat() if hooks["last_at"] else None}


# --------------------------------------------------------------------------
# Global search — P1-06
# --------------------------------------------------------------------------
# Every identifier the plan lists, and the one query each resolves through.
# `kind` is what the UI routes on; `route` is where it navigates. Declared as
# data so adding an identifier type is one row, not a new branch in a chain of
# ifs that each have to remember the merchant scope.
def _customer_row(row) -> tuple[str | None, str]:
    """Assemble a customer result from ciphertext.

    `customers.name` and `.email` are encrypted at rest (ADR-0052), and a raw
    SQL read returns exactly what is stored -- the ORM's type is not in play
    here. Decrypted at the edge, so the shape the caller sees is the same as
    every other search result.
    """
    from app.crypto import decrypt
    return (decrypt(row["label"], table="customers", column="name"),
            f'{decrypt(row["detail"], table="customers", column="email")} '
            f'\u00b7 {row["status"]}')


#: (kind, route, sql, shaper). The shaper is None where SQL alone produces the
#: `label` and `detail` a result needs, and a function where it cannot.
_SEARCHES: tuple[tuple[str, str, str, object], ...] = (
    # A payment resolves to its own lifecycle (§7), not to the incidents list.
    # Routing it there was a dead end: an operator pasting a payment id lands on
    # a page that does not contain it and has to start again.
    ("payment", "/payments/{id}", """
        SELECT id, id AS label, method AS detail, created_at
          FROM payments
         WHERE merchant_id = :m AND (id = :q OR external_payment_id = :q)
         LIMIT 5""", None),
    # An order and a customer resolve THROUGH their payments, because the
    # lifecycle is what somebody searching an order id actually wants -- "what
    # happened to this order" is a question about its payment. Ordered newest
    # first: an order with two attempts is asked about because of the last one.
    ("order", "/payments/{id}", """
        SELECT p.id, o.id AS label,
               o.status || ' · payment ' || p.status AS detail, p.created_at
          FROM orders o JOIN payments p ON p.order_id = o.id
         WHERE o.merchant_id = :m AND o.id = :q
         ORDER BY p.created_at DESC LIMIT 5""", None),
    # `label` and `detail` arrive as CIPHERTEXT here and are assembled in
    # `_customer_row` below. The other entries compose their detail in SQL with
    # `||`; this one cannot, because concatenating two encrypted values in the
    # database produces a string nothing can decrypt. See ADR-0052.
    ("customer", "/payments/{id}", """
        SELECT p.id, c.name AS label, c.email AS detail,
               p.status AS status, p.created_at
          FROM customers c JOIN payments p ON p.customer_id = c.id
         WHERE c.merchant_id = :m AND c.id = :q
         ORDER BY p.created_at DESC LIMIT 5""", _customer_row),
    ("incident", "/incidents/{id}", """
        SELECT id, title AS label, status AS detail, detected_at AS created_at
          FROM incidents WHERE merchant_id = :m AND id = :q LIMIT 5""", None),
    ("task", "/tasks/{id}", """
        SELECT id, request AS label, status AS detail, created_at
          FROM agent_tasks WHERE merchant_id = :m AND id = :q LIMIT 5""", None),
    ("action", "/actions", """
        SELECT id, action_type AS label, status AS detail, created_at
          FROM agent_actions
         WHERE merchant_id = :m AND (id = :q OR external_reference = :q)
         LIMIT 5""", None),
    # A provider reference is the identifier an operator arrives with when they
    # are looking at the provider's dashboard rather than at ours, which is
    # exactly the moment a search box earns its place.
    # The identifier an operator arrives with when they are looking at the
    # provider's dashboard rather than ours -- which is exactly the moment a
    # search box earns its place. It resolves to the payment's lifecycle, since
    # that is what a provider reference is a reference TO.
    ("provider_reference", "/payments/{id}", """
        SELECT a.target_payment_id AS id, a.external_reference AS label,
               a.action_type AS detail, a.created_at
          FROM agent_actions a
         WHERE a.merchant_id = :m
           AND (a.external_reference = :q OR a.external_payment_id = :q)
         LIMIT 5""", None),
)


def search(session, merchant_id: str, query: str) -> dict:
    """Resolve one identifier across every entity that can carry one.

    Exact match only, and deliberately so. A prefix or substring search over
    payment ids invites an operator to act on the row that happened to sort
    first, and every identifier in this system is something a person pastes
    rather than types. `find_duplicate_payments` is the tool for "which payments
    look alike"; this box answers "where is *this* one".

    Every branch is merchant-scoped in SQL. There is no result set here that
    was filtered in Python afterwards — a filter applied after the fact is a
    filter that can be forgotten, and forgetting it leaks another merchant's
    identifiers by their absence or presence.
    """
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": [], "truncated": False}

    results: list[dict] = []
    for kind, route, sql, shape in _SEARCHES:
        for row in session.execute(text(sql), {"m": merchant_id, "q": q}).mappings():
            label, detail = (shape(row) if shape else (row["label"], row["detail"]))
            results.append({
                "kind": kind,
                "id": row["id"],
                "label": label,
                "detail": detail,
                "created_at": (row["created_at"].isoformat()
                               if row["created_at"] else None),
                "route": route.replace("{id}", row["id"]),
            })

    return {"query": q, "results": results,
            # An identifier that matched nothing is a real answer and reads
            # differently from a search that was never run.
            "truncated": False}
