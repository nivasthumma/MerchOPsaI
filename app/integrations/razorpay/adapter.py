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
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.boundaries import assert_no_open_write
from app.config import get_settings
from app.integrations.razorpay.faults import (
    Fault,
    FaultInjector,
    ProviderAmbiguous,
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
    status: str                 # created | partially_paid | paid | expired | cancelled
    short_url: str
    # What the customer has actually paid against the link, as the provider
    # reports it. The only figure a link contributes to "recovered".
    amount_paid_minor: int = 0


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
    def find_refund_by_idempotency_key(self, key: str, *,
                                       external_payment_id: str | None = None
                                       ) -> ExternalRefund | None:
        """Reconciliation lookup. This is what makes UNKNOWN resolvable: after a
        timeout we hold no external reference, so the ONLY way to learn whether
        the action landed is to ask the provider about our own key.

        `external_payment_id` scopes the search. Razorpay cannot be asked about
        a key directly; it can list a payment's refunds, each carrying the key
        we wrote into its notes."""
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
        assert_no_open_write(self.session, "provider.create_refund")
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
        assert_no_open_write(self.session, "provider.get_refund")
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

    def find_refund_by_idempotency_key(self, key: str, *,
                                       external_payment_id: str | None = None
                                       ) -> ExternalRefund | None:
        assert_no_open_write(self.session, "provider.find_refund_by_idempotency_key")
        if self.injector.down:
            raise ProviderTimeout("Provider still unreachable during reconciliation.",
                                  submitted=True)
        return self.get_refund(self._refund_id(key))

    def get_payment(self, external_payment_id: str) -> ExternalPayment | None:
        assert_no_open_write(self.session, "provider.get_payment")
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
        assert_no_open_write(self.session, "provider.create_payment_link")
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
        assert_no_open_write(self.session, "provider.find_payment_link_by_idempotency_key")
        return self.get_payment_link(self._link_id(key))

    def find_notification_by_idempotency_key(self, key: str) -> ExternalNotification | None:
        assert_no_open_write(self.session, "provider.find_notification_by_idempotency_key")
        return self.get_notification(self._notification_id(key))

    def get_payment_link(self, link_id: str) -> ExternalPaymentLink | None:
        assert_no_open_write(self.session, "provider.get_payment_link")
        self.injector.apply("get_payment_link")
        r = self.session.execute(text(
            "SELECT id, amount_minor, status, short_url FROM payment_links WHERE id = :i"),
            {"i": link_id}).mappings().first()
        if r is None:
            return None
        return ExternalPaymentLink(id=r["id"], amount_minor=int(r["amount_minor"]),
                                   status=r["status"], short_url=r["short_url"],
                                   amount_paid_minor=(int(r["amount_minor"])
                                                      if r["status"] == "paid" else 0))

    def _notification_id(self, key: str) -> str:
        return "notif_MOCK" + hashlib.sha256(key.encode()).hexdigest()[:14].upper()

    def create_notification(self, *, merchant_id, customer_id, channel, template,
                            idempotency_key) -> ExternalNotification:
        assert_no_open_write(self.session, "provider.create_notification")
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
        assert_no_open_write(self.session, "provider.get_notification")
        self.injector.apply("get_notification")
        r = self.session.execute(text(
            "SELECT id, channel, status FROM notifications WHERE id = :i"),
            {"i": notification_id}).mappings().first()
        if r is None:
            return None
        return ExternalNotification(id=r["id"], channel=r["channel"], status=r["status"])


#: Razorpay's refund idempotency header. The refund APIs (normal and instant)
#: read THIS header; `X-Payment-Idempotency`, which this adapter used to send,
#: is not a refund header, so every live refund went out with no provider-side
#: idempotency at all and a retry after a lost response could refund twice.
REFUND_IDEMPOTENCY_HEADER = "X-Refund-Idempotency"

#: Razorpay: "at least 10 character long ... alphabets, numbers, hyphens and
#: underscores only". Checked before sending, because a key the provider
#: silently rejects is a request with no idempotency.
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{10,}$")

#: Payment Links cap `reference_id` at 40 characters. Our keys are 64-char
#: sha256 hex, which Razorpay refuses outright -- so the live payment-link path
#: could never have created a link. The first 40 hex characters are 160 bits
#: and still derived from server-held facts only.
PAYMENT_LINK_REFERENCE_MAX = 40

#: Refund listing is paged at 100. Bounded, because reconciliation is a sweep
#: and a sweep must terminate: 10 pages is 1,000 refunds against one payment.
_REFUND_PAGE = 100
_REFUND_PAGES_MAX = 10


def payment_link_reference(idempotency_key: str) -> str:
    """The `reference_id` a payment link is created with, and searched by."""
    return idempotency_key[:PAYMENT_LINK_REFERENCE_MAX]


def _error_description(resp) -> str:
    """Razorpay errors are `{"error": {"code", "description", ...}}`. Falls
    back to the raw body, truncated, when the body is not that shape."""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        # Not JSON, or JSON that is not an object: the raw body is all there is.
        return resp.text[:200]
    desc = (err.get("description") or "") if isinstance(err, dict) else ""
    code = (err.get("code") or "") if isinstance(err, dict) else ""
    if desc or code:
        return f"{code}: {desc}".strip(": ")
    return resp.text[:200]


def _notes(obj: dict) -> dict:
    # Razorpay renders empty notes as `[]` rather than `{}`.
    n = obj.get("notes")
    return n if isinstance(n, dict) else {}


class LiveTestModeAdapter(RazorpayAdapter):
    """Real Razorpay Test Mode. Activated only when credentials are present.

    ## How a response is read

    Every WRITE is classified into exactly one of three outcomes, because the
    caller's safety depends on which one it is:

      applied / refused   2xx with a readable body, or a 4xx that says the
                          request was rejected (invalid payment, over-refund,
                          not captured). Settled either way.
      not sent            the connection never opened -> ProviderTimeout
                          (submitted=False). Nothing can have happened.
      ambiguous           the request reached Razorpay and the answer does not
                          say what happened -> ProviderAmbiguous (submitted=True)
                          -> UNKNOWN -> reconciliation. Never FAILED, and never
                          retried, because FAILED invites a retry of something
                          that may already have moved money.

    Ambiguous covers: a read/write timeout, any 5xx, a 409 (the same key is
    still being processed, or was used for a different request), a 2xx body
    missing the fields we need, and a payment-link "reference ID already
    attempted" refusal, which is the provider telling us the first attempt
    exists.

    READS never raise httpx exceptions: a verifier that catches ProviderError
    and ProviderTimeout must not be crashed by an `HTTPStatusError` after the
    provider has accepted a refund.

    ## What is and is not verified here

    This class has contract tests against recorded response shapes
    (`tests/unit/test_razorpay_contract.py`). It has NOT been exercised against
    Razorpay Test Mode from this repository's CI: no credentials exist there.
    `scripts/razorpay_spike.py` is the path that does that, and records what it
    saw.
    """
    mode = "live_test_mode"
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, session, injector: FaultInjector | None = None, *, client=None):
        import httpx
        if client is None:
            s = get_settings()
            if not (s.razorpay_key_id and s.razorpay_key_secret):
                raise RuntimeError("Razorpay credentials are not configured.")
            # A short connect timeout: a connection that never opens is the one
            # failure that is provably "not sent", so it should surface fast.
            client = httpx.Client(
                base_url=self.BASE, timeout=httpx.Timeout(10.0, connect=5.0),
                auth=(s.razorpay_key_id, s.razorpay_key_secret),
            )
        self.session = session
        self.injector = injector or FaultInjector.disabled()
        self.last_fault: str | None = None
        self._client = client

    # --- transport -------------------------------------------------------
    def _send(self, method: str, path: str, *, operation: str, **kw):
        import httpx

        assert_no_open_write(self.session, f"provider.{operation}")
        try:
            return self._client.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise ProviderTimeout(
                f"{operation}: could not connect to the provider "
                f"({type(e).__name__}); the request was not sent.",
                submitted=False) from e
        except httpx.HTTPError as e:
            # Read/write timeouts, a reset mid-response, a protocol error: the
            # request may have been received and applied.
            raise ProviderTimeout(
                f"{operation}: {type(e).__name__} after the request was sent; the "
                f"provider may or may not have applied it.", submitted=True) from e

    def _write(self, path: str, *, operation: str, json: dict,
               headers: dict | None = None) -> dict:
        resp = self._send("POST", path, operation=operation, json=json,
                          headers=headers or {})
        if resp.status_code >= 500 or resp.status_code == 409:
            raise ProviderAmbiguous(
                f"{operation}: provider returned HTTP {resp.status_code} "
                f"({_error_description(resp)}); whether it was applied is unknown.",
                status_code=resp.status_code)
        if resp.status_code >= 400:
            desc = _error_description(resp)
            if "already attempted" in desc.lower():
                raise ProviderAmbiguous(
                    f"{operation}: provider reports this reference was already "
                    f"attempted ({desc}); the original may exist.",
                    status_code=resp.status_code)
            raise ProviderError(f"Provider rejected {operation}: {desc}",
                                code="EXTERNAL_API_ERROR")
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderAmbiguous(
                f"{operation}: HTTP {resp.status_code} with an unreadable body; "
                f"the request was accepted but its result cannot be read.",
                status_code=resp.status_code) from e
        if not isinstance(body, dict) or not body.get("id") or "amount" not in body:
            raise ProviderAmbiguous(
                f"{operation}: HTTP {resp.status_code} body is missing id/amount; "
                f"refusing to read it as success.", status_code=resp.status_code)
        return body

    def _read(self, path: str, *, operation: str, params: dict | None = None) -> dict | None:
        resp = self._send("GET", path, operation=operation, params=params or {})
        if resp.status_code == 404:
            return None
        if resp.status_code == 400 and "does not exist" in _error_description(resp).lower():
            # Razorpay answers an unknown id with 400, not 404.
            return None
        if resp.status_code >= 400:
            raise ProviderError(
                f"{operation}: provider returned HTTP {resp.status_code} "
                f"({_error_description(resp)}).", code="EXTERNAL_API_ERROR")
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError(f"{operation}: unreadable response body.",
                                code="EXTERNAL_API_ERROR") from e
        if not isinstance(body, dict):
            raise ProviderError(f"{operation}: unexpected response shape.",
                                code="EXTERNAL_API_ERROR")
        return body

    # --- refunds ---------------------------------------------------------
    @staticmethod
    def _refund(b: dict, fallback_payment_id: str = "") -> ExternalRefund:
        try:
            return ExternalRefund(id=b["id"], payment_id=b.get("payment_id") or fallback_payment_id,
                                  amount_minor=int(b["amount"]),
                                  status=b.get("status", "pending"),
                                  created_at=str(b.get("created_at", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(f"Malformed refund object: {e}",
                                code="EXTERNAL_API_ERROR") from e

    def create_refund(self, external_payment_id, amount_minor, idempotency_key) -> ExternalRefund:
        if not _IDEMPOTENCY_KEY.match(idempotency_key or ""):
            # Refused before anything is sent: nothing can have happened.
            raise ProviderError("Idempotency key does not meet the provider's format; "
                                "refusing to send a refund without one.",
                                code="EXTERNAL_API_ERROR")
        self.last_fault = self.injector.apply("create_refund")
        b = self._write(
            f"/payments/{external_payment_id}/refund", operation="create_refund",
            # The body must be byte-for-byte the same on a retry for Razorpay's
            # idempotency to hold; every field here is derived, none is clocked.
            json={"amount": amount_minor, "speed": "normal",
                  "notes": {"idempotency_key": idempotency_key}},
            headers={REFUND_IDEMPOTENCY_HEADER: idempotency_key},
        )
        try:
            return self._refund(b, external_payment_id)
        except ProviderError as e:
            raise ProviderAmbiguous(f"create_refund: {e}") from e

    def get_refund(self, refund_id: str) -> ExternalRefund | None:
        self.injector.apply("get_refund")
        b = self._read(f"/refunds/{refund_id}", operation="get_refund")
        return None if b is None else self._refund(b)

    def find_refund_by_idempotency_key(self, key: str, *,
                                       external_payment_id: str | None = None
                                       ) -> ExternalRefund | None:
        """List the payment's refunds and match the key recorded in `notes`.

        Without the payment id there is nothing to list, and None means "not
        established", which every caller already treats as UNKNOWN rather
        than as "did not happen".
        """
        if not external_payment_id:
            return None
        for page in range(_REFUND_PAGES_MAX):
            b = self._read(f"/payments/{external_payment_id}/refunds",
                           operation="list_refunds",
                           params={"count": _REFUND_PAGE, "skip": page * _REFUND_PAGE})
            items = (b or {}).get("items") or []
            for item in items:
                if _notes(item).get("idempotency_key") == key:
                    return self._refund(item, external_payment_id)
            if len(items) < _REFUND_PAGE:
                return None
        raise ProviderError(
            f"More than {_REFUND_PAGE * _REFUND_PAGES_MAX} refunds on "
            f"{external_payment_id}; the key could not be searched exhaustively.",
            code="EXTERNAL_API_ERROR")

    def get_payment(self, external_payment_id: str) -> ExternalPayment | None:
        self.injector.apply("get_payment")
        b = self._read(f"/payments/{external_payment_id}", operation="get_payment")
        if b is None:
            return None
        try:
            return ExternalPayment(
                id=b["id"], amount_minor=int(b["amount"]),
                amount_refunded_minor=int(b.get("amount_refunded") or 0),
                refund_status=b.get("refund_status"), status=b.get("status", "unknown"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(f"Malformed payment object: {e}",
                                code="EXTERNAL_API_ERROR") from e

    # --- §18 recovery actions --------------------------------------------
    @staticmethod
    def _link(b: dict) -> ExternalPaymentLink:
        try:
            return ExternalPaymentLink(id=b["id"], amount_minor=int(b["amount"]),
                                       status=b.get("status", "created"),
                                       short_url=b.get("short_url", ""),
                                       amount_paid_minor=int(b.get("amount_paid") or 0))
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(f"Malformed payment link object: {e}",
                                code="EXTERNAL_API_ERROR") from e

    def create_payment_link(self, *, merchant_id, customer_id, amount_minor,
                            source_payment_id, idempotency_key) -> ExternalPaymentLink:
        self.last_fault = self.injector.apply("create_payment_link")
        b = self._write("/payment_links", operation="create_payment_link", json={
            "amount": amount_minor, "currency": "INR",
            # Razorpay's only duplicate protection for links: a reference it
            # has seen before is refused, and the refusal is read as ambiguous.
            "reference_id": payment_link_reference(idempotency_key),
            "notes": {"idempotency_key": idempotency_key,
                      "source_payment_id": source_payment_id or ""},
        })
        try:
            return self._link(b)
        except ProviderError as e:
            raise ProviderAmbiguous(f"create_payment_link: {e}") from e

    def get_payment_link(self, link_id: str) -> ExternalPaymentLink | None:
        self.injector.apply("get_payment_link")
        b = self._read(f"/payment_links/{link_id}", operation="get_payment_link")
        return None if b is None else self._link(b)

    def find_payment_link_by_idempotency_key(self, key: str) -> ExternalPaymentLink | None:
        """Search by the `reference_id` the link was created with. This used to
        return None unconditionally, which left every payment link whose
        creation response was lost UNKNOWN until escalation."""
        ref = payment_link_reference(key)
        b = self._read("/payment_links", operation="list_payment_links",
                       params={"reference_id": ref})
        for item in (b or {}).get("payment_links") or []:
            if item.get("reference_id") == ref:
                return self._link(item)
        return None

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

    def find_notification_by_idempotency_key(self, key: str) -> ExternalNotification | None:
        """No notification channel exists on this path, so there is nothing to ask."""
        return None


def get_adapter(session, injector: FaultInjector | None = None) -> RazorpayAdapter:
    mode = get_settings().resolved_razorpay_mode
    if mode == "live_test_mode":
        return LiveTestModeAdapter(session, injector)
    return MockAdapter(session, injector)
