"""One payment, end to end — MerchantOps §7.

The plan asks for one correlation chain:

    request_id → task_id → agent_run_id → tool_call_id → action_id
      → provider_reference → webhook_event_id → verification_id

and states the point of it plainly:

> A payment should be traceable through its complete lifecycle.

`trace_by_correlation` already answers "everything ONE OPERATION touched", and
that is a different question. A payment's life spans several operations with
several correlation ids: detection raised an incident under one, an
investigation ran under another, a webhook arrived under a third, the
reconciliation sweep settled it under a fourth. Nothing joined them, so the
chain existed in four pieces and the question an operator actually asks —
*a customer is on the phone about this payment, what happened to it* — could
only be answered by four queries and a guess.

## How the pieces are found

Everything hangs off two identifiers, and getting from one to the other is the
mapping layer's job (never a guess):

    internal payment id  ──(provider_mappings)──▶  external payment id

From those:

    refunds              payment_id
    recovery candidates  payment_id ──▶ plan ──▶ incident
    actions              target_payment_id, or either external id
    tasks                action.task_id, plus tasks whose incident matches
    approvals            action.approval_id
    webhook events       entity_id = external payment id or external reference
    audit                every correlation id the above collected

## What it is not

Not a narrative. Every entry is a row that exists, with its own timestamp, and
the ordering is chronological rather than an assumed sequence — an out-of-order
webhook genuinely arrives out of order, and sorting it into the position it
"should" have had would hide the thing worth seeing.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

# The stages of the chain, in the order §7 lists them. Published with the
# response so a client renders the chain's shape from the system's own
# declaration rather than from a copy that can drift.
STAGES = ("payment", "mapping", "incident", "investigation", "tool_call",
          "approval", "action", "provider_event", "verification", "refund")


def _at(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def payment_lifecycle(session, merchant_id: str, payment_id: str) -> dict | None:
    """Everything that ever touched one payment. `None` if it is not this
    merchant's — the caller turns that into a 404 rather than leaking existence.

    One transaction, several queries. Not one query: the joins fan out in
    different directions and a single statement would multiply rows against
    each other, which is how a payment with three refunds and two actions comes
    to report six of each.
    """
    payment = session.execute(text("""
        SELECT p.id, p.merchant_id, p.order_id, p.customer_id, p.amount_minor,
               p.currency, p.method, p.status, p.error_reason,
               p.amount_refunded_minor, p.refund_status, p.created_at,
               c.name AS customer_name, c.email AS customer_email
          FROM payments p
          LEFT JOIN customers c ON c.id = p.customer_id
         WHERE p.id = :p
    """), {"p": payment_id}).mappings().first()

    if payment is None or payment["merchant_id"] != merchant_id:
        return None

    events: list[dict] = [{
        "stage": "payment",
        "at": _at(payment["created_at"]),
        "id": payment["id"],
        "label": f"Payment {payment['status']}",
        "detail": (f"{payment['method']} · "
                   f"{payment['currency']} {payment['amount_minor'] / 100:,.2f}"
                   + (f" · {payment['error_reason']}" if payment["error_reason"] else "")),
        "correlation_id": None,
    }]

    # --- the mapping, which is what makes every provider-side lookup possible
    mapping = session.execute(text("""
        SELECT id, provider, environment, external_payment_id, status, source,
               verified_at, created_at
          FROM provider_mappings
         WHERE payment_id = :p AND status = 'ACTIVE'
    """), {"p": payment_id}).mappings().first()

    external_ids: set[str] = set()
    if mapping:
        external_ids.add(mapping["external_payment_id"])
        events.append({
            "stage": "mapping",
            "at": _at(mapping["created_at"]),
            "id": mapping["id"],
            "label": f"Mapped to {mapping['provider']} ({mapping['environment']})",
            "detail": mapping["external_payment_id"]
                      + ("" if mapping["verified_at"]
                         else " · never confirmed against the provider"),
            "correlation_id": None,
        })

    correlations: set[str] = set()

    # --- actions ----------------------------------------------------------
    actions = [dict(r) for r in session.execute(text("""
        SELECT a.id, a.task_id, a.action_type, a.status, a.amount_minor,
               a.external_reference, a.external_payment_id,
               a.verification_state, a.verification_detail, a.verify_attempts,
               a.escalated, a.escalated_at, a.last_verified_at, a.next_verify_at,
               a.approval_id, a.created_at, a.updated_at
          FROM agent_actions a
         WHERE a.merchant_id = :m
           AND (a.target_payment_id = :p
                OR (a.external_payment_id IS NOT NULL
                    AND a.external_payment_id = ANY(:ext)))
         ORDER BY a.created_at
    """), {"m": merchant_id, "p": payment_id,
           "ext": list(external_ids) or [""]}).mappings().all()]

    task_ids = {a["task_id"] for a in actions if a["task_id"]}
    for a in actions:
        if a["external_reference"]:
            external_ids.add(a["external_reference"])

    # --- incidents that NAME this payment ---------------------------------
    #
    # A duplicate-payment incident is about this payment and says so in its
    # signals, but it reaches the payment through a recovery candidate that
    # only exists once somebody plans a recovery. Before that, the incident and
    # the payment were unconnected — so opening the payment showed no incident
    # while the incidents list showed one about it.
    #
    # Matched on the QUOTED json value (`"SYN_PAY_0002"`) rather than a bare
    # substring: `%SYN_PAY_0002%` would also match `SYN_PAY_00020`, and an
    # operator being shown another payment's incident is worse than being shown
    # none. A degradation incident names a method rather than payments and is
    # correctly not matched here.
    named_incident_ids = set(session.execute(text("""
        SELECT id FROM incidents
         WHERE merchant_id = :m AND signals::text LIKE :needle
    """), {"m": merchant_id, "needle": f'%"{payment_id}"%'}).scalars())

    # --- candidates, and the incident/plan they came from ------------------
    candidates = [dict(r) for r in session.execute(text("""
        SELECT c.id, c.plan_id, c.incident_id, c.intervention, c.status,
               c.attributed_amount_minor, c.expected_recovery_minor,
               c.actual_recovery_minor, c.task_id, c.created_at
          FROM recovery_candidates c
         WHERE c.merchant_id = :m AND c.payment_id = :p
         ORDER BY c.created_at
    """), {"m": merchant_id, "p": payment_id}).mappings().all()]
    incident_ids = ({c["incident_id"] for c in candidates if c["incident_id"]}
                    | named_incident_ids)
    task_ids |= {c["task_id"] for c in candidates if c["task_id"]}

    # --- tasks -------------------------------------------------------------
    tasks = []
    if task_ids:
        tasks = [dict(r) for r in session.execute(text("""
            SELECT t.id, t.request, t.status, t.intent, t.incident_id,
                   t.user_id, t.tool_call_count, t.duration_ms, t.created_at,
                   t.model_provider, t.model_version, t.is_replay
              FROM agent_tasks t
             WHERE t.merchant_id = :m AND t.id = ANY(:ids)
             ORDER BY t.created_at
        """), {"m": merchant_id, "ids": list(task_ids)}).mappings().all()]
        incident_ids |= {t["incident_id"] for t in tasks if t["incident_id"]}

    # --- incidents ---------------------------------------------------------
    incidents = []
    if incident_ids:
        incidents = [dict(r) for r in session.execute(text("""
            SELECT i.id, i.incident_type, i.severity, i.status, i.title,
                   i.detection_rule, i.correlation_id, i.detected_at
              FROM incidents i
             WHERE i.merchant_id = :m AND i.id = ANY(:ids)
             ORDER BY i.detected_at
        """), {"m": merchant_id, "ids": list(incident_ids)}).mappings().all()]
        correlations |= {i["correlation_id"] for i in incidents if i["correlation_id"]}

    for i in incidents:
        events.append({
            "stage": "incident", "at": _at(i["detected_at"]), "id": i["id"],
            "label": f"{i['incident_type'].replace('_', ' ').title()} detected",
            "detail": f"{i['severity']} · {i['detection_rule']}",
            "correlation_id": i["correlation_id"],
        })

    for t in tasks:
        events.append({
            "stage": "investigation", "at": _at(t["created_at"]), "id": t["id"],
            "label": "Replay" if t["is_replay"] else "Investigation started",
            "detail": f"{t['request'][:120]} · {t['tool_call_count']} tool calls "
                      f"· {t['model_provider'] or 'deterministic'}",
            "correlation_id": None,
        })

    # --- tool calls, but only the ones that touched THIS payment ----------
    #
    # A revenue investigation makes a dozen calls, most of them about the
    # merchant rather than about one transaction. Listing all of them here
    # would bury the payment's own story in the surrounding investigation's.
    if task_ids:
        for r in session.execute(text("""
            SELECT tc.id, tc.task_id, tc.seq, tc.tool_name, tc.success,
                   tc.error_code, tc.policy_decision, tc.created_at, tc.input
              FROM tool_calls tc
             WHERE tc.task_id = ANY(:ids)
               AND (tc.input::text LIKE :needle
                    OR tc.input::text LIKE ANY(:ext_needles))
             ORDER BY tc.created_at
        """), {"ids": list(task_ids), "needle": f'%"{payment_id}"%',
               "ext_needles": [f'%"{e}"%' for e in external_ids] or ["%\x00%"]},
        ).mappings():
            # A gated call is not a failed one, and saying "failed" next to a
            # refund that went on to succeed is simply wrong. `request_refund`
            # returns success=False with no error code and
            # policy_decision=REQUIRE_APPROVAL when the control plane stops it
            # for a human — the tool did exactly what it should.
            if r["success"]:
                outcome = "ok"
            elif r["error_code"]:
                outcome = r["error_code"]
            elif r["policy_decision"] and r["policy_decision"] != "ALLOW":
                outcome = f"held by policy: {r['policy_decision']}"
            else:
                outcome = "failed"
            events.append({
                "stage": "tool_call", "at": _at(r["created_at"]), "id": r["id"],
                "label": f"{r['tool_name']}",
                "detail": outcome,
                "correlation_id": None,
            })

    # --- approvals ---------------------------------------------------------
    approval_ids = {a["approval_id"] for a in actions if a["approval_id"]}
    if approval_ids:
        for r in session.execute(text("""
            SELECT ap.id, ap.decision, ap.risk_level, ap.decided_by,
                   ap.decided_at, ap.created_at, ap.required_signatures
              FROM approvals ap
             WHERE ap.merchant_id = :m AND ap.id = ANY(:ids)
             ORDER BY ap.created_at
        """), {"m": merchant_id, "ids": list(approval_ids)}).mappings():
            events.append({
                "stage": "approval",
                "at": _at(r["decided_at"] or r["created_at"]), "id": r["id"],
                "label": f"Approval {r['decision'].lower()}",
                "detail": (f"{r['risk_level']} risk · "
                           + (f"by {r['decided_by']}" if r["decided_by"]
                              else f"awaiting {r['required_signatures']} signature(s)")),
                "correlation_id": None,
            })

    for a in actions:
        events.append({
            "stage": "action", "at": _at(a["created_at"]), "id": a["id"],
            "label": f"{a['action_type'].replace('_', ' ').title()} sent to provider",
            "detail": (f"{a['amount_minor'] / 100:,.2f} · "
                       + (a["external_reference"] or "no reference issued")),
            "correlation_id": None,
        })
        # Verification is its own link in §7's chain, and its own moment: an
        # action submitted at 12:00 and verified at 12:04 is two events, and
        # collapsing them hides the gap that UNKNOWN lives in.
        if a["verification_state"]:
            events.append({
                "stage": "verification",
                "at": _at(a["last_verified_at"] or a["updated_at"]),
                "id": a["id"],
                "label": f"Verification: {a['verification_state']}",
                "detail": ((a["verification_detail"] or {}).get("reason") or "")[:160]
                          + (f" · attempt {a['verify_attempts']}"
                             if a["verify_attempts"] else ""),
                "correlation_id": None,
            })
        if a["escalated"]:
            events.append({
                "stage": "verification", "at": _at(a["escalated_at"]), "id": a["id"],
                "label": "Escalated to a person",
                "detail": "Automatic reconciliation exhausted.",
                "correlation_id": None,
            })

    # --- provider events ---------------------------------------------------
    if external_ids:
        for r in session.execute(text("""
            SELECT w.id, w.event_id, w.event_type, w.status, w.signature_valid,
                   w.received_at, w.processed_at, w.processing_note,
                   w.correlation_id
              FROM webhook_events w
             WHERE w.entity_id = ANY(:ext)
             ORDER BY w.received_at
        """), {"ext": list(external_ids)}).mappings():
            if r["correlation_id"]:
                correlations.add(r["correlation_id"])
            events.append({
                "stage": "provider_event", "at": _at(r["received_at"]),
                "id": r["event_id"],
                "label": f"Provider event: {r['event_type']}",
                "detail": (f"{r['status']}"
                           + ("" if r["signature_valid"] else " · SIGNATURE NOT VERIFIED")
                           + (f" · {r['processing_note'][:100]}"
                              if r["processing_note"] else "")),
                "correlation_id": r["correlation_id"],
            })

    # --- refunds -----------------------------------------------------------
    for r in session.execute(text("""
        SELECT id, amount_minor, status, external_reference, created_at
          FROM refunds WHERE merchant_id = :m AND payment_id = :p
         ORDER BY created_at
    """), {"m": merchant_id, "p": payment_id}).mappings():
        events.append({
            "stage": "refund", "at": _at(r["created_at"]), "id": r["id"],
            "label": f"Refund {r['status']}",
            "detail": f"{r['amount_minor'] / 100:,.2f} · "
                      + (r["external_reference"] or "no provider reference"),
            "correlation_id": None,
        })

    # Chronological, and stable where timestamps tie. Deliberately NOT sorted
    # into the order the stages "should" occur: an out-of-order webhook really
    # did arrive out of order, and tidying it into place hides the one thing
    # worth seeing.
    events.sort(key=lambda e: (e["at"] or "", STAGES.index(e["stage"])))

    return {
        "payment": {
            "id": payment["id"], "merchant_id": payment["merchant_id"],
            "order_id": payment["order_id"], "customer_id": payment["customer_id"],
            "customer_name": payment["customer_name"],
            "amount_minor": payment["amount_minor"], "currency": payment["currency"],
            "method": payment["method"], "status": payment["status"],
            "error_reason": payment["error_reason"],
            "amount_refunded_minor": payment["amount_refunded_minor"],
            "refund_status": payment["refund_status"],
            "created_at": _at(payment["created_at"]),
        },
        "external_payment_id": mapping["external_payment_id"] if mapping else None,
        "provider": mapping["provider"] if mapping else None,
        "environment": mapping["environment"] if mapping else None,
        "events": events,
        "stages": list(STAGES),
        # The correlation ids this payment's story spans. The whole reason this
        # module exists is that there is more than one, so the number is worth
        # showing rather than hiding behind a single "trace" link.
        "correlation_ids": sorted(c for c in correlations if c),
        "incident_ids": sorted(incident_ids),
        "task_ids": sorted(task_ids),
        "action_ids": [a["id"] for a in actions],
        "generated_at": datetime.now(UTC).isoformat(),
    }
