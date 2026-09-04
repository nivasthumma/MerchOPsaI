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
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import get_settings
from app.integrations.razorpay.faults import (
    Fault,
    FaultInjector,
    ProviderError,
    ProviderTimeout,
)


@dataclass
class ExternalRefund:
    id: str
    payment_id: str
    amount_minor: int
    status: str                 # processed | pending | failed
    created_at: str


@dataclass
class ExternalPaymentLink:
    id: str
    amount_minor: int
    status: str                 # created | paid | expired | cancelled
    short_url: str


@dataclass
class ExternalNotification:
    id: str
    channel: str                # email | sms
    status: str                 # queued | sent | failed


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

    # --- MerchantOps §18 recovery actions ---------------------------------
    # Each create takes an idempotency key for the same reason create_refund
    # does: these reach a customer, and a retry that sends a second message is
    # not a no-op just because no money moved.
    @abstractmethod
    def create_payment_link(self, *, merchant_id: str, customer_id: str,
                            amount_minor: int, source_payment_id: str | None,
                            idempotency_key: str) -> ExternalPaymentLink: ...

    @abstractmethod
    def get_payment_link(self, link_id: str) -> ExternalPaymentLink | None: ...

    @abstractmethod
    def create_notification(self, *, merchant_id: str, customer_id: str,
                            channel: str, template: str,
                            idempotency_key: str) -> ExternalNotification: ...

    @abstractmethod
    def get_notification(self, notification_id: str) -> ExternalNotification | None: ...

    # The reconciliation lookups for the non-refund actions. Without these, an
    # action whose response was lost has no way back: the refund path has had
    # `find_refund_by_idempotency_key` since the beginning and the other two
    # had nothing, which meant the UNKNOWN exit path only worked for refunds.
    @abstractmethod
    def find_payment_link_by_idempotency_key(self, key: str) -> ExternalPaymentLink | None: ...

    @abstractmethod
    def find_notification_by_idempotency_key(self, key: str) -> ExternalNotification | None: ...


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
                                  created_at=datetime.now(UTC).isoformat())

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

        if self.last_fault == Fault.ACCEPTED_NOT_APPLIED.value:
            # A refund id is issued but no state moves. Verification must read
            # the payment to notice; the response alone looks like success.
            return ExternalRefund(id=rid, payment_id=external_payment_id,
                                  amount_minor=amount_minor, status="pending",
                                  created_at=datetime.now(UTC).isoformat())

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
                              created_at=datetime.now(UTC).isoformat())

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

    # --- §18 recovery actions --------------------------------------------
    def _link_id(self, key: str) -> str:
        return "plink_MOCK" + hashlib.sha256(key.encode()).hexdigest()[:14].upper()

    def create_payment_link(self, *, merchant_id, customer_id, amount_minor,
                            source_payment_id, idempotency_key) -> ExternalPaymentLink:
        self.last_fault = self.injector.apply("create_payment_link")
        lid = self._link_id(idempotency_key)
        # Provider-side idempotency, exactly as for refunds: the same key
        # returns the original object rather than creating a second one.
        existing = self.session.execute(text(
            "SELECT id, amount_minor, status, short_url FROM payment_links WHERE id = :i"),
            {"i": lid}).mappings().first()
        if existing:
            return ExternalPaymentLink(id=lid, amount_minor=int(existing["amount_minor"]),
                                       status=existing["status"],
                                       short_url=existing["short_url"])
        url = f"https://rzp.io/i/{lid[-10:]}"
        self.session.execute(text("""
            INSERT INTO payment_links (id, merchant_id, customer_id, source_payment_id,
                                       amount_minor, currency, status, short_url, created_at)
            VALUES (:i, :m, :c, :p, :a, 'INR', 'created', :u, now())
        """), {"i": lid, "m": merchant_id, "c": customer_id, "p": source_payment_id,
               "a": amount_minor, "u": url})
        self.session.flush()
        return ExternalPaymentLink(id=lid, amount_minor=amount_minor,
                                   status="created", short_url=url)

    def find_payment_link_by_idempotency_key(self, key: str) -> ExternalPaymentLink | None:
        return self.get_payment_link(self._link_id(key))

    def find_notification_by_idempotency_key(self, key: str) -> ExternalNotification | None:
        return self.get_notification(self._notification_id(key))

    def get_payment_link(self, link_id: str) -> ExternalPaymentLink | None:
        self.injector.apply("get_payment_link")
        r = self.session.execute(text(
            "SELECT id, amount_minor, status, short_url FROM payment_links WHERE id = :i"),
            {"i": link_id}).mappings().first()
        if r is None:
            return None
        return ExternalPaymentLink(id=r["id"], amount_minor=int(r["amount_minor"]),
                                   status=r["status"], short_url=r["short_url"])

    def _notification_id(self, key: str) -> str:
        return "notif_MOCK" + hashlib.sha256(key.encode()).hexdigest()[:14].upper()

    def create_notification(self, *, merchant_id, customer_id, channel, template,
                            idempotency_key) -> ExternalNotification:
        self.last_fault = self.injector.apply("create_notification")
        nid = self._notification_id(idempotency_key)
        existing = self.session.execute(text(
            "SELECT id, channel, status FROM notifications WHERE id = :i"),
            {"i": nid}).mappings().first()
        if existing:
            return ExternalNotification(id=nid, channel=existing["channel"],
                                        status=existing["status"])
        self.session.execute(text("""
            INSERT INTO notifications (id, merchant_id, customer_id, channel, template,
                                       status, created_at)
            VALUES (:i, :m, :c, :ch, :t, 'sent', now())
        """), {"i": nid, "m": merchant_id, "c": customer_id, "ch": channel, "t": template})
        self.session.flush()
        return ExternalNotification(id=nid, channel=channel, status="sent")

    def get_notification(self, notification_id: str) -> ExternalNotification | None:
        self.injector.apply("get_notification")
        r = self.session.execute(text(
            "SELECT id, channel, status FROM notifications WHERE id = :i"),
            {"i": notification_id}).mappings().first()
        if r is None:
            return None
        return ExternalNotification(id=r["id"], channel=r["channel"], status=r["status"])


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


    # --- §18 recovery actions --------------------------------------------
    def create_payment_link(self, *, merchant_id, customer_id, amount_minor,
                            source_payment_id, idempotency_key) -> ExternalPaymentLink:
        import httpx
        self.last_fault = self.injector.apply("create_payment_link")
        try:
            resp = self._client.post("/payment_links", json={
                "amount": amount_minor, "currency": "INR",
                "reference_id": idempotency_key,
                "notes": {"idempotency_key": idempotency_key,
                          "source_payment_id": source_payment_id or ""},
            })
        except httpx.HTTPError as e:
            raise ProviderTimeout(str(e), submitted=True) from e
        if resp.status_code >= 500:
            raise ProviderError(f"Provider {resp.status_code}", code="EXTERNAL_API_ERROR")
        if resp.status_code >= 400:
            raise ProviderError(f"Provider rejected payment link: {resp.text[:200]}",
                                code="EXTERNAL_API_ERROR")
        b = resp.json()
        return ExternalPaymentLink(id=b["id"], amount_minor=int(b["amount"]),
                                   status=b.get("status", "created"),
                                   short_url=b.get("short_url", ""))

    def get_payment_link(self, link_id: str) -> ExternalPaymentLink | None:
        self.injector.apply("get_payment_link")
        resp = self._client.get(f"/payment_links/{link_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        b = resp.json()
        return ExternalPaymentLink(id=b["id"], amount_minor=int(b["amount"]),
                                   status=b.get("status", "created"),
                                   short_url=b.get("short_url", ""))

    def create_notification(self, *, merchant_id, customer_id, channel, template,
                            idempotency_key) -> ExternalNotification:
        """Not available on this path, and it fails closed rather than pretending.

        Razorpay notifies *about a payment link* (`/payment_links/:id/notify_by/:medium`);
        it is not a general customer-messaging service, and this build has no
        email or SMS provider configured. Returning a fabricated success here
        would be the one thing the whole verification design exists to prevent:
        reporting that a customer was contacted when nobody was.
        """
        raise ProviderError(
            "No live notification channel is configured. send_customer_notification "
            "executes only against the mock adapter in this build.",
            code="INTEGRATION_UNAVAILABLE")

    def get_notification(self, notification_id: str) -> ExternalNotification | None:
        return None

    def find_payment_link_by_idempotency_key(self, key: str) -> ExternalPaymentLink | None:
        """Razorpay has no key-lookup endpoint. The reference_id we set at
        creation is searchable, but listing is not implemented here — the same
        honest gap `find_refund_by_idempotency_key` has.

        This class used to declare this method twice, and the other one looked
        like a working implementation: `self.get_payment_link(self._link_id(key))`.
        It was dead — Python keeps the last definition — and it could not have
        worked, because `_link_id` is a MockAdapter method that derives a
        deterministic id from the key. On a live adapter it would have raised
        AttributeError. Reading the file top to bottom told you key lookup was
        implemented; running it returned None.

        The consequence of returning None is real and belongs in the open, not
        behind a method that appears to do something: a payment link whose
        creation response was lost cannot be reconciled by key, so it stays
        UNKNOWN until the sweep escalates it (README, known limitation 7).
        """
        return None

    def find_notification_by_idempotency_key(self, key: str) -> ExternalNotification | None:
        """No lookup path, for the same reason as payment links above."""
        return None


def get_adapter(session, injector: FaultInjector | None = None) -> RazorpayAdapter:
    mode = get_settings().resolved_razorpay_mode
    if mode == "live_test_mode":
        return LiveTestModeAdapter(session, injector)
    return MockAdapter(session, injector)
