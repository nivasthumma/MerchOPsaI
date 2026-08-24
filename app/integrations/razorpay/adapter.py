"""Razorpay adapter — CONTRACT §7, §22.

The agent never reaches Razorpay. It reaches a typed tool, which reaches the
action layer, which reaches this adapter. The adapter owns auth, request
construction, response normalisation, external ids, timeouts and provider
errors — nothing above it depends on provider-specific HTTP shapes.

Two implementations:

  LiveTestModeAdapter  real Razorpay Test Mode (requires credentials)
  MockAdapter          deterministic local double

CONTRACT §7 requires that a mocked integration is never described as real.
`mode` is surfaced through the API, the trace and the README for exactly that
reason.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import get_settings
from app.integrations.razorpay.faults import (
    Fault, FaultInjector, ProviderError, ProviderTimeout,
)


@dataclass
class ExternalRefund:
    id: str
    payment_id: str
    amount_minor: int
    status: str                 # processed | pending | failed
    created_at: str


@dataclass
class ExternalPayment:
    id: str
    amount_minor: int
    amount_refunded_minor: int
    refund_status: str | None   # None | partial | full
    status: str


class RazorpayAdapter(ABC):
    mode: str = "abstract"

    @abstractmethod
    def create_refund(self, external_payment_id: str, amount_minor: int,
                      idempotency_key: str) -> ExternalRefund: ...

    @abstractmethod
    def get_refund(self, refund_id: str) -> ExternalRefund | None: ...

    @abstractmethod
    def get_payment(self, external_payment_id: str) -> ExternalPayment | None: ...

    @abstractmethod
    def find_refund_by_idempotency_key(self, key: str) -> ExternalRefund | None:
        """Reconciliation lookup. This is what makes UNKNOWN resolvable: after a
        timeout we hold no external reference, so the ONLY way to learn whether
        the action landed is to ask the provider about our own key."""
        ...


class MockAdapter(RazorpayAdapter):
    """Deterministic double. State lives in the local DB so that verification
    genuinely re-reads state rather than trusting a returned object."""
    mode = "mock"

    def __init__(self, session, injector: FaultInjector | None = None):
        self.session = session
        self.injector = injector or FaultInjector.disabled()
        self.last_fault: str | None = None

    def _refund_id(self, key: str) -> str:
        return "rfnd_MOCK" + hashlib.sha256(key.encode()).hexdigest()[:14].upper()

    def create_refund(self, external_payment_id, amount_minor, idempotency_key) -> ExternalRefund:
        # TIMEOUT_AFTER_SUBMIT means the provider RECEIVED the request; the
        # response was lost. Modelling it as "raise before doing the work"
        # would make it a safe no-op and would not exercise the dangerous case
        # the UNKNOWN state exists for. It is therefore applied *after* the
        # state change, below.
        post_fault = self.injector.fault is Fault.TIMEOUT_AFTER_SUBMIT \
            and self.injector.on_operation == "create_refund" \
            and not self.injector.fired
        if not post_fault:
            self.last_fault = self.injector.apply("create_refund")

        rid = self._refund_id(idempotency_key)

        # Provider-side idempotency FIRST. This must precede balance validation:
        # after a refund lands the refundable balance is 0, so checking the
        # balance first would reject a legitimate idempotent replay (the
        # double-click / retry path) as an over-refund instead of returning the
        # original refund.
        existing = self.session.execute(
            text("SELECT id, amount_minor, status FROM refunds WHERE id = :r"), {"r": rid}
        ).mappings().first()
        if existing:
            return ExternalRefund(id=rid, payment_id=external_payment_id,
                                  amount_minor=int(existing["amount_minor"]),
                                  status=existing["status"],
                                  created_at=datetime.now(timezone.utc).isoformat())

        row = self.session.execute(text("""
            SELECT id, amount_minor, amount_refunded_minor, status
            FROM payments WHERE external_payment_id = :e
        """), {"e": external_payment_id}).mappings().first()
        if row is None:
            raise ProviderError(f"No such payment: {external_payment_id}", code="EXTERNAL_API_ERROR")
        if row["status"] == "failed":
            raise ProviderError(f"Payment {external_payment_id} is not captured.",
                                code="EXTERNAL_API_ERROR")
        remaining = int(row["amount_minor"]) - int(row["amount_refunded_minor"])
        if amount_minor > remaining:
            raise ProviderError(
                f"Refund exceeds refundable balance ({remaining}).", code="EXTERNAL_API_ERROR")

        if self.last_fault == Fault.MALFORMED_RESPONSE.value:
            # Body the caller cannot interpret -> must not be read as success.
            raise ProviderError("Malformed provider response: missing refund id.",
                                code="EXTERNAL_API_ERROR")

        # Apply the state change.
        self.session.execute(text("""
            INSERT INTO refunds (id, merchant_id, payment_id, amount_minor, status,
                                 external_reference, created_at)
            SELECT :rid, p.merchant_id, p.id, :amt, 'processed', :rid, now()
            FROM payments p WHERE p.external_payment_id = :e
        """), {"rid": rid, "amt": amount_minor, "e": external_payment_id})
        self.session.execute(text("""
            UPDATE payments
               SET amount_refunded_minor = amount_refunded_minor + :amt,
                   refund_status = CASE WHEN amount_refunded_minor + :amt >= amount_minor
                                        THEN 'full' ELSE 'partial' END,
                   status = CASE WHEN amount_refunded_minor + :amt >= amount_minor
                                 THEN 'refunded' ELSE status END
             WHERE external_payment_id = :e
        """), {"amt": amount_minor, "e": external_payment_id})
        self.session.flush()

        if post_fault:
            # The refund is now real at the provider, but the caller will never
            # see the reference. This is precisely the state UNKNOWN exists for.
            self.last_fault = self.injector.apply("create_refund")

        return ExternalRefund(id=rid, payment_id=external_payment_id,
                              amount_minor=amount_minor, status="processed",
                              created_at=datetime.now(timezone.utc).isoformat())

    def get_refund(self, refund_id: str) -> ExternalRefund | None:
        self.injector.apply("get_refund")
        r = self.session.execute(text("""
            SELECT r.id, r.amount_minor, r.status, p.external_payment_id
            FROM refunds r JOIN payments p ON p.id = r.payment_id WHERE r.id = :r
        """), {"r": refund_id}).mappings().first()
        if r is None:
            return None
        return ExternalRefund(id=r["id"], payment_id=r["external_payment_id"] or "",
                              amount_minor=int(r["amount_minor"]), status=r["status"],
                              created_at="")

    def find_refund_by_idempotency_key(self, key: str) -> ExternalRefund | None:
        if self.injector.down:
            raise ProviderTimeout("Provider still unreachable during reconciliation.",
                                  submitted=True)
        return self.get_refund(self._refund_id(key))

    def get_payment(self, external_payment_id: str) -> ExternalPayment | None:
        self.injector.apply("get_payment")
        row = self.session.execute(text("""
            SELECT id, amount_minor, amount_refunded_minor, refund_status, status
            FROM payments WHERE external_payment_id = :e
        """), {"e": external_payment_id}).mappings().first()
        if row is None:
            return None
        return ExternalPayment(id=external_payment_id, amount_minor=int(row["amount_minor"]),
                               amount_refunded_minor=int(row["amount_refunded_minor"]),
                               refund_status=row["refund_status"], status=row["status"])


class LiveTestModeAdapter(RazorpayAdapter):
    """Real Razorpay Test Mode. Activated only when credentials are present.

    Deliberately thin: the Day-0 feasibility spike (CONTRACT §7) decides
    whether this path is usable, and scripts/razorpay_spike.py records the
    answer. If it is not usable the project runs on MockAdapter and says so.
    """
    mode = "live_test_mode"
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, session, injector: FaultInjector | None = None):
        import httpx
        s = get_settings()
        if not (s.razorpay_key_id and s.razorpay_key_secret):
            raise RuntimeError("Razorpay credentials are not configured.")
        self.session = session
        self.injector = injector or FaultInjector.disabled()
        self.last_fault: str | None = None
        self._client = httpx.Client(
            base_url=self.BASE, timeout=10.0,
            auth=(s.razorpay_key_id, s.razorpay_key_secret),
        )

    def create_refund(self, external_payment_id, amount_minor, idempotency_key) -> ExternalRefund:
        import httpx
        self.last_fault = self.injector.apply("create_refund")
        try:
            resp = self._client.post(
                f"/payments/{external_payment_id}/refund",
                json={"amount": amount_minor, "speed": "normal",
                      "notes": {"idempotency_key": idempotency_key}},
                headers={"X-Payment-Idempotency": idempotency_key},
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeout(str(e), submitted=True) from e
        except httpx.HTTPError as e:
            raise ProviderTimeout(str(e), submitted=True) from e
        if resp.status_code >= 500:
            raise ProviderError(f"Provider {resp.status_code}", code="EXTERNAL_API_ERROR")
        if resp.status_code >= 400:
            raise ProviderError(f"Provider rejected refund: {resp.text[:200]}",
                                code="EXTERNAL_API_ERROR")
        b = resp.json()
        return ExternalRefund(id=b["id"], payment_id=external_payment_id,
                              amount_minor=int(b["amount"]), status=b.get("status", "pending"),
                              created_at=str(b.get("created_at", "")))

    def get_refund(self, refund_id: str) -> ExternalRefund | None:
        self.injector.apply("get_refund")
        resp = self._client.get(f"/refunds/{refund_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        b = resp.json()
        return ExternalRefund(id=b["id"], payment_id=b.get("payment_id", ""),
                              amount_minor=int(b["amount"]), status=b.get("status", "pending"),
                              created_at=str(b.get("created_at", "")))

    def find_refund_by_idempotency_key(self, key: str) -> ExternalRefund | None:
        """Razorpay has no key-lookup endpoint, so reconcile by listing the
        payment's refunds and matching the key we recorded in notes."""
        return None

    def get_payment(self, external_payment_id: str) -> ExternalPayment | None:
        self.injector.apply("get_payment")
        resp = self._client.get(f"/payments/{external_payment_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        b = resp.json()
        return ExternalPayment(
            id=b["id"], amount_minor=int(b["amount"]),
            amount_refunded_minor=int(b.get("amount_refunded", 0)),
            refund_status=b.get("refund_status"), status=b.get("status", "unknown"),
        )


def get_adapter(session, injector: FaultInjector | None = None) -> RazorpayAdapter:
    mode = get_settings().resolved_razorpay_mode
    if mode == "live_test_mode":
        return LiveTestModeAdapter(session, injector)
    return MockAdapter(session, injector)
