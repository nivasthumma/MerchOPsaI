"""Deterministic planner — a stand-in for the model, not a model.

WHY THIS EXISTS
    1. The evaluation suite must run without credentials and produce identical
       results on every run (CONTRACT §30).
    2. It isolates what is being measured. With this provider, a failing
       scenario is a HARNESS defect — policy, verification, idempotency,
       isolation — not model variance.

WHAT IT IS NOT
    It is not a language model and it does not reason. Metrics produced with
    `llm_provider=deterministic` measure the control plane around the agent,
    NOT agent intelligence. CONTRACT §54 forbids conflating the two, so the
    provider name is recorded on every task and printed on every report.

It plans from the request text and the tool results observed so far -- the same
information the model gets -- and deliberately ignores any instruction found
inside tool output, which is where injected content lives.
"""
from __future__ import annotations

import re

from app.llm.base import LLMProvider, LLMTurn, ToolRequest


def _first_sentence(text: str, limit: int = 200) -> str:
    m = re.search(r"(?<=[.!?])\s", text)
    return (text[:m.start()] if m else text)[:limit].strip()


DUPLICATE_RE = re.compile(r"\bduplicate|duplicat|double[- ]charg|charged twice\b", re.I)
REFUND_RE = re.compile(r"\brefund|reimburse|money back\b", re.I)
REVENUE_RE = re.compile(r"\brevenue|sales|turnover|income|drop|decline|fell|down\b", re.I)
FAILURE_RE = re.compile(r"\bfail|failure|declin|success rate|payment method\b", re.I)
ORDER_RE = re.compile(r"\b(SYN_ORD_[A-Z0-9]+)\b")
PAYMENT_RE = re.compile(r"\b(SYN_PAY_[0-9]+)\b")
AMOUNT_RE = re.compile(r"\bamount\s+([0-9]{2,})\b", re.I)
SHOW_ORDER_RE = re.compile(r"\b(show|get|fetch|open|display|look up)\b.*\border\b", re.I)
# MerchantOps §18 added nine tools. These route to them, and each is gated
# narrowly enough not to change where an existing request already goes -- the
# planner is what the whole evaluation suite measures through, so a broad
# trigger here silently rewrites what a hundred scenarios are testing.
REASON_RE = re.compile(r"\b(error|reason|breakdown|root cause)\b", re.I)
CUSTOMER_RE = re.compile(r"\b(SYN_CUS_[A-Z0-9]+)\b")
INCIDENT_RE = re.compile(r"\b(INC_[A-Z0-9]+)\b")
ACTION_RE = re.compile(r"\b(ACT_[A-Z0-9]+)\b")
PROVIDER_RE = re.compile(r"\b(provider|external state|reconcile|reconciliation|webhook)\b", re.I)
PAYMENT_LINK_RE = re.compile(r"\bpayment link\b", re.I)


class DeterministicProvider(LLMProvider):
    name = "deterministic"
    model = "deterministic-planner-v1"

    def turn(self, *, system: str, messages: list[dict], tools: list[dict],
             timeout: float | None = None) -> LLMTurn:
        """Plan the next step, and on the last one attach §37's output block.

        `timeout` is accepted and ignored: this planner is local arithmetic, so
        there is nothing for a deadline to interrupt. Dropping the parameter
        would make the two providers differ in signature for no reason.
        """
        turn = self._plan(system=system, messages=messages, tools=tools)
        if turn.wants_tools or not turn.text:
            return turn
        turn.text = turn.text + "\n\n" + self._output_block(
            self._first_user_text(messages), messages, turn.text)
        return turn

    # ------------------------------------------------------------------
    def _output_block(self, request: str, messages: list[dict], prose: str) -> str:
        """MerchantOps §37, built from what the tools actually returned.

        Every field is derived. `confidence` in particular is computed from the
        evidence actually gathered rather than asserted -- a planner that hard-
        coded 0.9 would be teaching the evaluation suite to accept a number
        nothing produced, which is the same objection §18 raises to a hard-coded
        duplicate confidence.
        """
        import json as _json

        results = self._tool_results(messages)
        # Evidence ids run across successful calls in order, matching exactly how
        # AgentRuntime._render_tool_result numbered them for the model.
        ids: list[str] = []
        n = 0
        for r in results:
            if not r.get("success"):
                continue
            for _ in r.get("evidence", []):
                n += 1
                ids.append(f"E{n}")

        wants_dupe = bool(DUPLICATE_RE.search(request))
        wants_refund = bool(REFUND_RE.search(request))
        wants_link = bool(PAYMENT_LINK_RE.search(request))
        called = self._tools_called(messages)
        proposed_action = bool(called & {"request_refund", "generate_payment_link",
                                         "send_customer_notification"})

        if wants_dupe or wants_refund:
            intent = "duplicate_payment"
        elif wants_link:
            intent = "payment_recovery"
        elif REVENUE_RE.search(request) or FAILURE_RE.search(request):
            intent = "revenue_investigation"
        else:
            intent = "record_lookup"

        findings = []
        if ids:
            findings.append({
                "type": "root_cause" if intent in ("revenue_investigation",
                                                   "duplicate_payment") else "observation",
                # First SENTENCE, not first period: "-6.29%" is one number and
                # splitting on "." alone truncated the claim mid-figure.
                "claim": _first_sentence(prose) or "See the answer above.",
                # Cite what was read, not everything: a finding that cites every
                # id is a finding that cites nothing in particular.
                "evidence_ids": ids[:5],
            })
        else:
            findings.append({
                "type": "uncertainty",
                "claim": "No evidence was gathered, so no claim is supported.",
                "evidence_ids": [],
            })

        recommendation = None
        if proposed_action:
            recommendation = {
                "type": "refund_duplicate" if "request_refund" in called
                        else "payment_link_recovery",
                "detail": "Proposed for human approval; policy decides whether it proceeds.",
            }
        elif intent == "revenue_investigation" and ids:
            recommendation = {"type": "investigate_further",
                              "detail": "The degraded method warrants a recovery plan."}

        # Computed, and deliberately capped below certainty: a deterministic
        # planner reading five numbers has not earned 1.0.
        confidence = round(min(0.9, 0.4 + 0.1 * len(ids)), 2) if ids else 0.1

        return "```json\n" + _json.dumps({
            "intent": intent,
            "findings": findings,
            "recommendation": recommendation,
            "confidence": confidence,
            # A proposed financial action always wants a person. Policy has
            # already said so; agreeing costs nothing and disagreeing would be
            # ignored anyway.
            "requires_human": proposed_action,
        }, indent=2) + "\n```"

    # ------------------------------------------------------------------
    def _plan(self, *, system: str, messages: list[dict], tools: list[dict],
              timeout: float | None = None) -> LLMTurn:
        # Mirrors `turn` because doubles extend the planner by forwarding
        # `turn`'s keywords straight here. Named rather than swallowed with
        # **kwargs so a misspelled argument is still an error.
        available = {t["name"] for t in tools}
        request = self._first_user_text(messages)
        called = self._tools_called(messages)
        results = self._tool_results(messages)

        # Explicit "show me order X" -- must route to get_order so that the
        # merchant-scope check actually runs on the requested resource.
        explicit_order_ref = ORDER_RE.search(request)
        if explicit_order_ref and SHOW_ORDER_RE.search(request) \
                and not DUPLICATE_RE.search(request):
            oid = explicit_order_ref.group(1)
            if "get_order" in available and not self._called_with(
                    messages, "get_order", {"order_id": oid}):
                return self._call("get_order", {"order_id": oid})
            return LLMTurn(text=self._order_summary(results, oid), stop_reason="end_turn")

        # ---------------- recovery contact (§18) --------------------------
        # The model may PROPOSE contacting a customer. Whether it is permitted,
        # and whether a human signs it off, is decided downstream — which is
        # what makes this branch worth having: without it the authorization
        # scenarios assert that a tool nobody calls was not called.
        if PAYMENT_LINK_RE.search(request) and "generate_payment_link" in available \
                and "generate_payment_link" not in called:
            pay = PAYMENT_RE.search(request)
            if pay:
                return self._call("generate_payment_link", {
                    "synthetic_payment_id": pay.group(1),
                    "reason": "Payment failed during a period of method degradation; "
                              "offering the customer another route to complete it.",
                })

        # ---------------- direct lookups (§18) ----------------------------
        # Placed before the tracks below: a request naming one entity is asking
        # about that entity, not opening an investigation.
        direct = self._direct_lookup(request, available, messages, called, results)
        if direct is not None:
            return direct

        wants_dupe = bool(DUPLICATE_RE.search(request))
        wants_refund = bool(REFUND_RE.search(request))
        wants_revenue = bool(REVENUE_RE.search(request)) or bool(FAILURE_RE.search(request))

        # ---------------- duplicate / refund track ------------------------
        if wants_dupe or wants_refund:
            if "find_duplicate_payments" in available and "find_duplicate_payments" not in called:
                return self._call("find_duplicate_payments", {"window_seconds": 600})

            pairs = self._duplicate_pairs(results)
            explicit_order = ORDER_RE.search(request)
            order_id = explicit_order.group(1) if explicit_order else (
                pairs[0]["order_id"] if pairs else None)

            if order_id and "get_order" in available and not self._called_with(
                    messages, "get_order", {"order_id": order_id}):
                return self._call("get_order", {"order_id": order_id})

            if wants_refund and "request_refund" in available and "request_refund" not in called:
                target, amount = self._refund_target(request, pairs, results)
                if target:
                    return self._call("request_refund", {
                        "synthetic_payment_id": target,
                        "amount_minor": amount,
                        "reason": ("Duplicate payment: a second capture was recorded for the "
                                   "same order, customer and amount within a short interval."),
                    })

            return LLMTurn(text=self._duplicate_summary(pairs), stop_reason="end_turn")

        # ---------------- revenue / failure investigation -----------------
        if wants_revenue:
            if "get_revenue_summary" in available and "get_revenue_summary" not in called:
                return self._call("get_revenue_summary", {})
            if "get_payment_metrics" in available and not self._called_with(
                    messages, "get_payment_metrics", {"method": None}):
                return self._call("get_payment_metrics", {"method": None})

            worst = self._worst_method(results)
            if worst and "get_payment_metrics" in available and not self._called_with(
                    messages, "get_payment_metrics", {"method": worst}):
                return self._call("get_payment_metrics", {"method": worst})

            # Asking WHY something failed is a different question from asking
            # WHICH method failed, and §18 gives it its own tool.
            if REASON_RE.search(request) and "get_failure_breakdown" in available \
                    and "get_failure_breakdown" not in called:
                return self._call("get_failure_breakdown", {"method": worst})

            return LLMTurn(text=self._revenue_summary(results, worst), stop_reason="end_turn")

        # ---------------- fallback ----------------------------------------
        if "get_revenue_summary" in available and "get_revenue_summary" not in called:
            return self._call("get_revenue_summary", {})
        return LLMTurn(
            text="I could not map this request onto an available investigation tool.",
            stop_reason="end_turn")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _call(name: str, args: dict) -> LLMTurn:
        return LLMTurn(text="", stop_reason="tool_use",
                       tool_requests=[ToolRequest(id=f"det_{name}_{abs(hash(str(args))) % 10**6}",
                                                  name=name, arguments=args)])

    @staticmethod
    def _first_user_text(messages: list[dict]) -> str:
        for m in messages:
            if m["role"] == "user":
                c = m["content"]
                if isinstance(c, str):
                    return c
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        return b["text"]
        return ""

    @staticmethod
    def _tools_called(messages: list[dict]) -> set[str]:
        out = set()
        for m in messages:
            if m["role"] == "assistant" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        out.add(b["name"])
        return out

    @staticmethod
    def _called_with(messages: list[dict], name: str, args: dict) -> bool:
        for m in messages:
            if m["role"] == "assistant" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use" \
                            and b["name"] == name and b.get("input") == args:
                        return True
        return False

    @staticmethod
    def _tool_results(messages: list[dict]) -> list[dict]:
        """Structured tool payloads only. Free text inside them is never read
        as an instruction -- only named numeric/id fields are consulted."""
        out = []
        for m in messages:
            if m["role"] == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        payload = b.get("_structured")
                        if isinstance(payload, dict):
                            out.append(payload)
        return out

    @staticmethod
    def _duplicate_pairs(results: list[dict]) -> list[dict]:
        for r in results:
            if "pairs" in r.get("data", {}):
                return r["data"]["pairs"]
        return []

    def _refund_target(self, request: str, pairs: list[dict], results: list[dict]):
        explicit = PAYMENT_RE.search(request)
        if explicit:
            pid = explicit.group(1)
            # An amount stated in the request is authoritative for the request;
            # policy still decides whether it is permissible.
            stated = AMOUNT_RE.search(request)
            if stated:
                return pid, int(stated.group(1))
            for r in results:
                for p in r.get("data", {}).get("payments", []):
                    if p["id"] == pid:
                        return pid, p["amount_minor"] - p.get("amount_refunded_minor", 0)
            for p in pairs:
                if p["second_payment_id"] == pid or p["first_payment_id"] == pid:
                    return pid, p["amount_minor"]
            return pid, 0
        if pairs:
            # Refund the LATER capture: the first is the legitimate payment.
            return pairs[0]["second_payment_id"], pairs[0]["amount_minor"]
        return None, 0

    @staticmethod
    def _worst_method(results: list[dict]) -> str | None:
        for r in results:
            methods = r.get("data", {}).get("by_method")
            if methods:
                scored = [m for m in methods if m.get("delta_pct_points") is not None]
                if not scored:
                    return None
                worst = min(scored, key=lambda m: m["delta_pct_points"])
                return worst["method"] if worst["delta_pct_points"] < 0 else None
        return None

    @staticmethod
    def _order_summary(results: list[dict], order_id: str) -> str:
        for r in results:
            o = r.get("data", {}).get("order")
            if o and o.get("id") == order_id:
                return (f"Order {o['id']}: status {o['status']}, "
                        f"INR {o['amount_minor']/100:,.2f}, "
                        f"{len(r['data'].get('payments', []))} payment(s).")
        return f"Order {order_id} could not be retrieved."

    @staticmethod
    def _duplicate_summary(pairs: list[dict]) -> str:
        if not pairs:
            return "No duplicate payments were found within the search window."
        p = pairs[0]
        return (f"Found {len(pairs)} likely duplicate payment(s). On order {p['order_id']}, "
                f"payments {p['first_payment_id']} and {p['second_payment_id']} both captured "
                f"INR {p['amount_minor']/100:,.2f} for the same customer "
                f"{p['time_separation_seconds']} seconds apart (confidence {p['confidence']}).")

    def _direct_lookup(self, request, available, messages, called, results):
        """Route a request that names a specific entity straight at it.

        A request that ASKS FOR AN ACTION is never a lookup, whatever entities
        it happens to mention. The recovery planner dispatches with text like
        "Refund payment X ... for incident INC_Y", and without this guard the
        incident id pulled the whole thing into a read and no refund was ever
        proposed. Four tests caught it; the scenario suite did not, because no
        scenario names an incident id.
        """
        if REFUND_RE.search(request) or DUPLICATE_RE.search(request):
            return None

        cust = CUSTOMER_RE.search(request)
        if cust and "get_customer" in available:
            cid = cust.group(1)
            if not self._called_with(messages, "get_customer", {"customer_id": cid}):
                return self._call("get_customer", {"customer_id": cid})
            return LLMTurn(text=self._entity_summary(results, "customer"),
                           stop_reason="end_turn")

        inc = INCIDENT_RE.search(request)
        if inc and "calculate_recovery_candidates" in available:
            iid = inc.group(1)
            if not self._called_with(messages, "calculate_recovery_candidates",
                                     {"incident_id": iid}):
                return self._call("calculate_recovery_candidates", {"incident_id": iid})
            return LLMTurn(text=self._entity_summary(results, "recovery"),
                           stop_reason="end_turn")

        act = ACTION_RE.search(request)
        if act and PROVIDER_RE.search(request):
            aid = act.group(1)
            if "reconcile_transaction" in available and "reconcile_transaction" not in called:
                return self._call("reconcile_transaction", {"action_id": aid})
            return LLMTurn(text=self._entity_summary(results, "reconcile"),
                           stop_reason="end_turn")

        pay = PAYMENT_RE.search(request)
        if pay and not REFUND_RE.search(request) and not DUPLICATE_RE.search(request):
            pid = pay.group(1)
            # "what does the provider say about X" is a question about external
            # state, and answering it from our own records would substitute one
            # for the other (§32).
            if PROVIDER_RE.search(request):
                if "get_payment_status" in available and "get_payment_status" not in called:
                    return self._call("get_payment_status", {"payment_id": pid})
            elif "get_payment" in available and not self._called_with(
                    messages, "get_payment", {"payment_id": pid}):
                return self._call("get_payment", {"payment_id": pid})
            else:
                return None
            return LLMTurn(text=self._entity_summary(results, "payment"),
                           stop_reason="end_turn")
        return None

    @staticmethod
    def _entity_summary(results: list[dict], kind: str) -> str:
        """State what the tools returned. Deliberately flat: the planner has no
        judgement to add, and prose that sounded like analysis would be prose
        nothing produced."""
        data = results[-1].get("data", {}) if results else {}
        if not data:
            return "The requested record could not be read."
        if kind == "customer":
            return (f"Customer {data.get('id')} ({data.get('segment')}): "
                    f"{data.get('payments')} payments, {data.get('failed_payments')} failed, "
                    f"lifetime paid INR {data.get('lifetime_paid_minor', 0)/100:,.2f}. "
                    f"Contact opted out: {data.get('contact_opted_out')}.")
        if kind == "recovery":
            return (f"Incident {data.get('incident_id')}: intervention "
                    f"{data.get('intervention')}, {data.get('eligible_count')} of "
                    f"{data.get('candidate_count')} candidates eligible. Expected recovery "
                    f"INR {data.get('expected_recovery_minor', 0)/100:,.2f} — "
                    f"{data.get('expected_recovery_basis', '')}")
        if kind == "reconcile":
            return (f"Action {data.get('action_id')} reconciled: "
                    f"{data.get('from')} -> {data.get('to')}. {data.get('reason', '')}")
        if "provider_status" in data:
            return (f"Provider reports payment {data.get('external_payment_id')} as "
                    f"{data.get('provider_status')}, refunded INR "
                    f"{data.get('provider_amount_refunded_minor', 0)/100:,.2f}. "
                    f"Internal and provider agree: {data.get('internal_and_provider_agree')}.")
        return (f"Payment {data.get('id')}: {data.get('status')}, INR "
                f"{data.get('amount_minor', 0)/100:,.2f} via {data.get('method')}, "
                f"refundable balance INR "
                f"{data.get('refundable_balance_minor', 0)/100:,.2f}."
                + (f" Error: {data['error_reason']}." if data.get("error_reason") else ""))

    @staticmethod
    def _revenue_summary(results: list[dict], worst: str | None) -> str:
        rev = next((r["data"] for r in results if "change_pct" in r.get("data", {})), None)
        met = next((r["data"] for r in results if "by_method" in r.get("data", {})), None)
        hourly = next((r["data"].get("hourly_breakdown") for r in results
                       if r.get("data", {}).get("hourly_breakdown")), None)
        if not rev:
            return "Insufficient evidence was collected to explain the revenue change."
        parts = [
            f"Revenue moved {rev['change_pct']}% period-over-period "
            f"(INR {rev['previous_period_revenue_minor']/100:,.2f} -> "
            f"INR {rev['current_period_revenue_minor']/100:,.2f})."
        ]
        if worst and met:
            m = next(x for x in met["by_method"] if x["method"] == worst)
            parts.append(
                f"The decline is concentrated in {worst}: success fell from "
                f"{m['previous_success_rate_pct']}% to {m['current_success_rate_pct']}% "
                f"({m['delta_pct_points']} percentage points), while other methods held steady.")
        brk = next((r["data"] for r in results if "by_reason" in r.get("data", {})), None)
        if brk and brk.get("by_reason"):
            top = brk["by_reason"][0]
            parts.append(
                f"The dominant error is {top['error_reason']} ({top['count']} occurrences, "
                f"{top['share_pct']}% of failures, INR {top['value_minor']/100:,.2f}).")
        if hourly and hourly.get("worst_hours"):
            hrs = ", ".join(f"{h['hour']:02d}:00 ({h['failure_rate_pct']}% failed)"
                            for h in hourly["worst_hours"])
            parts.append(f"Failures cluster in specific hours: {hrs}.")
            if hourly.get("top_errors"):
                e = hourly["top_errors"][0]
                parts.append(f"The dominant error is {e['error_reason']} ({e['count']} occurrences).")
        return " ".join(parts)
