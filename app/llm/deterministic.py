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

DUPLICATE_RE = re.compile(r"\bduplicate|duplicat|double[- ]charg|charged twice\b", re.I)
REFUND_RE = re.compile(r"\brefund|reimburse|money back\b", re.I)
REVENUE_RE = re.compile(r"\brevenue|sales|turnover|income|drop|decline|fell|down\b", re.I)
FAILURE_RE = re.compile(r"\bfail|failure|declin|success rate|payment method\b", re.I)
ORDER_RE = re.compile(r"\b(SYN_ORD_[A-Z0-9]+)\b")
PAYMENT_RE = re.compile(r"\b(SYN_PAY_[0-9]+)\b")
AMOUNT_RE = re.compile(r"\bamount\s+([0-9]{2,})\b", re.I)
SHOW_ORDER_RE = re.compile(r"\b(show|get|fetch|open|display|look up)\b.*\border\b", re.I)


class DeterministicProvider(LLMProvider):
    name = "deterministic"
    model = "deterministic-planner-v1"

    def turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
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
        if hourly and hourly.get("worst_hours"):
            hrs = ", ".join(f"{h['hour']:02d}:00 ({h['failure_rate_pct']}% failed)"
                            for h in hourly["worst_hours"])
            parts.append(f"Failures cluster in specific hours: {hrs}.")
            if hourly.get("top_errors"):
                e = hourly["top_errors"][0]
                parts.append(f"The dominant error is {e['error_reason']} ({e['count']} occurrences).")
        return " ".join(parts)
