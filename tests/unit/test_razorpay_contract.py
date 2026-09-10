"""Contract tests for the live Razorpay adapter, against recorded response shapes.

These drive `LiveTestModeAdapter` through `httpx.MockTransport`, so they prove
what the adapter SENDS and how it READS each documented response. They do not
prove Razorpay Test Mode behaves this way today -- that needs credentials and
`scripts/razorpay_spike.py`, and nothing here claims otherwise.

Shapes follow Razorpay's API reference: refunds (create normal, idempotent
request, fetch multiple for a payment) and Payment Links (create, fetch all).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.razorpay.adapter import (
    PAYMENT_LINK_REFERENCE_MAX,
    REFUND_IDEMPOTENCY_HEADER,
    LiveTestModeAdapter,
    payment_link_reference,
)
from app.integrations.razorpay.faults import (
    ProviderAmbiguous,
    ProviderError,
    ProviderTimeout,
)

KEY = "a" * 24 + "0123456789abcdef0123456789abcdef0123456789ab"   # 64 chars, like sha256 hex
PAY = "pay_29QQoUBi66xm2f"


def _refund_body(rid="rfnd_FP8QHiV938haTz", amount=500100, key=KEY, status="processed"):
    return {"id": rid, "entity": "refund", "amount": amount, "currency": "INR",
            "payment_id": PAY, "notes": {"idempotency_key": key},
            "created_at": 1597078866, "status": status, "speed_requested": "normal"}


def _error(code, desc):
    return {"error": {"code": code, "description": desc, "source": "business",
                      "step": "payment_initiation", "reason": "input_validation_failed"}}


class Recorder:
    """A transport that answers from a handler and remembers every request."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        return self.handler(request)

    def adapter(self) -> LiveTestModeAdapter:
        client = httpx.Client(base_url=LiveTestModeAdapter.BASE,
                              transport=httpx.MockTransport(self))
        return LiveTestModeAdapter(None, client=client)

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


# ------------------------------------------------------------------ refunds
def test_success_sends_the_refund_idempotency_header_and_reads_the_refund():
    rec = Recorder(lambda r: httpx.Response(200, json=_refund_body()))
    ref = rec.adapter().create_refund(PAY, 500100, KEY)

    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == f"/v1/payments/{PAY}/refund"
    assert req.headers[REFUND_IDEMPOTENCY_HEADER] == KEY
    # The header the adapter used to send is not a refund header.
    assert "X-Payment-Idempotency" not in req.headers
    body = json.loads(req.content)
    assert body == {"amount": 500100, "speed": "normal", "notes": {"idempotency_key": KEY}}
    assert (ref.id, ref.amount_minor, ref.status) == ("rfnd_FP8QHiV938haTz", 500100, "processed")


def test_idempotent_retry_sends_the_same_key_and_body_and_gets_the_same_refund():
    """Razorpay returns the original refund for a repeated key with the same
    body. The adapter's half of that contract is sending an identical request."""
    rec = Recorder(lambda r: httpx.Response(200, json=_refund_body()))
    a = rec.adapter()
    first, second = a.create_refund(PAY, 500100, KEY), a.create_refund(PAY, 500100, KEY)
    assert first.id == second.id
    r1, r2 = rec.posts()
    assert r1.headers[REFUND_IDEMPOTENCY_HEADER] == r2.headers[REFUND_IDEMPOTENCY_HEADER] == KEY
    assert r1.content == r2.content, "a retry with a different body is a new request to Razorpay"


def test_duplicate_while_first_is_in_flight_is_ambiguous_not_failed():
    rec = Recorder(lambda r: httpx.Response(409, json=_error(
        "BAD_REQUEST_ERROR", "Request is already being processed")))
    with pytest.raises(ProviderAmbiguous) as e:
        rec.adapter().create_refund(PAY, 500100, KEY)
    assert e.value.submitted is True and e.value.status_code == 409


def test_same_key_different_request_is_ambiguous_not_failed():
    """The original request under this key WAS processed; money may have moved."""
    rec = Recorder(lambda r: httpx.Response(409, json=_error(
        "BAD_REQUEST_ERROR",
        "Different request with the same idempotency key has already been processed")))
    with pytest.raises(ProviderAmbiguous):
        rec.adapter().create_refund(PAY, 500100, KEY)


def test_timeout_before_submit_is_not_submitted():
    def handler(r):
        raise httpx.ConnectTimeout("connect timed out", request=r)
    with pytest.raises(ProviderTimeout) as e:
        Recorder(handler).adapter().create_refund(PAY, 500100, KEY)
    assert e.value.submitted is False
    assert not isinstance(e.value, ProviderAmbiguous)


def test_connection_refused_is_not_submitted():
    def handler(r):
        raise httpx.ConnectError("connection refused", request=r)
    with pytest.raises(ProviderTimeout) as e:
        Recorder(handler).adapter().create_refund(PAY, 500100, KEY)
    assert e.value.submitted is False


def test_timeout_after_submit_is_submitted():
    def handler(r):
        raise httpx.ReadTimeout("read timed out", request=r)
    with pytest.raises(ProviderTimeout) as e:
        Recorder(handler).adapter().create_refund(PAY, 500100, KEY)
    assert e.value.submitted is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_provider_5xx_is_ambiguous_not_failed(status):
    rec = Recorder(lambda r: httpx.Response(status, json=_error("SERVER_ERROR", "oops")))
    with pytest.raises(ProviderAmbiguous) as e:
        rec.adapter().create_refund(PAY, 500100, KEY)
    assert e.value.submitted is True and e.value.status_code == status


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>gateway</html>"),              # not JSON
    httpx.Response(200, json={"entity": "refund", "amount": 1}),    # no id
    httpx.Response(200, json={"id": "rfnd_X", "entity": "refund"}),  # no amount
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"id": "rfnd_X", "amount": "lots"}),   # amount unreadable
])
def test_malformed_success_body_is_ambiguous_never_success(response):
    rec = Recorder(lambda r: response)
    with pytest.raises(ProviderAmbiguous):
        rec.adapter().create_refund(PAY, 500100, KEY)


def test_invalid_payment_is_a_definite_refusal():
    rec = Recorder(lambda r: httpx.Response(400, json=_error(
        "BAD_REQUEST_ERROR", f"{PAY} is not a valid id")))
    with pytest.raises(ProviderError) as e:
        rec.adapter().create_refund(PAY, 500100, KEY)
    assert not isinstance(e.value, ProviderTimeout)
    assert "not a valid id" in str(e.value)


def test_over_refund_is_a_definite_refusal():
    rec = Recorder(lambda r: httpx.Response(400, json=_error(
        "BAD_REQUEST_ERROR", "The refund amount provided is greater than amount captured")))
    with pytest.raises(ProviderError) as e:
        rec.adapter().create_refund(PAY, 999999, KEY)
    assert "greater than amount captured" in str(e.value)


def test_a_key_the_provider_would_reject_is_refused_before_sending():
    rec = Recorder(lambda r: httpx.Response(200, json=_refund_body()))
    for bad in ("short", "has space in it here", "", "semi;colon;key;x"):
        with pytest.raises(ProviderError):
            rec.adapter().create_refund(PAY, 500100, bad)
    assert rec.requests == [], "nothing may be sent without a valid idempotency key"


# ------------------------------------------------------------ reconciliation
def test_find_refund_by_key_lists_the_payments_refunds_and_matches_notes():
    other = _refund_body(rid="rfnd_OTHER", key="z" * 64)
    ours = _refund_body(rid="rfnd_OURS")
    empty_notes = dict(_refund_body(rid="rfnd_BARE"), notes=[])   # Razorpay renders {} as []

    def handler(r):
        assert r.url.path == f"/v1/payments/{PAY}/refunds"
        return httpx.Response(200, json={"entity": "collection", "count": 3,
                                         "items": [other, empty_notes, ours]})
    found = Recorder(handler).adapter().find_refund_by_idempotency_key(
        KEY, external_payment_id=PAY)
    assert found is not None and found.id == "rfnd_OURS"


def test_find_refund_by_key_pages_until_a_short_page():
    def handler(r):
        skip = int(r.url.params["skip"])
        if skip == 0:
            items = [_refund_body(rid=f"rfnd_{i}", key="y" * 64) for i in range(100)]
        else:
            items = [_refund_body(rid="rfnd_LATE")]
        return httpx.Response(200, json={"entity": "collection", "count": len(items),
                                         "items": items})
    rec = Recorder(handler)
    found = rec.adapter().find_refund_by_idempotency_key(KEY, external_payment_id=PAY)
    assert found.id == "rfnd_LATE" and len(rec.requests) == 2


def test_find_refund_by_key_without_a_payment_is_not_established():
    rec = Recorder(lambda r: httpx.Response(500))
    assert rec.adapter().find_refund_by_idempotency_key(KEY) is None
    assert rec.requests == []


def test_find_refund_by_key_returns_none_when_absent():
    rec = Recorder(lambda r: httpx.Response(200, json={"entity": "collection",
                                                       "count": 0, "items": []}))
    assert rec.adapter().find_refund_by_idempotency_key(KEY, external_payment_id=PAY) is None


# ---------------------------------------------------------------- reads
def test_unknown_payment_reads_as_none_not_an_http_exception():
    """Razorpay answers an unknown id with 400 'does not exist', not 404."""
    rec = Recorder(lambda r: httpx.Response(400, json=_error(
        "BAD_REQUEST_ERROR", "The id provided does not exist")))
    assert rec.adapter().get_payment("pay_NOPE") is None


def test_read_5xx_raises_provider_error_not_httpx():
    """The verifier catches ProviderError/ProviderTimeout. An HTTPStatusError
    escaping from here would crash verification after the refund landed."""
    rec = Recorder(lambda r: httpx.Response(503))
    with pytest.raises(ProviderError):
        rec.adapter().get_payment(PAY)
    with pytest.raises(ProviderError):
        rec.adapter().get_refund("rfnd_X")


def test_get_payment_reads_refunded_amount():
    rec = Recorder(lambda r: httpx.Response(200, json={
        "id": PAY, "entity": "payment", "amount": 500100, "amount_refunded": 500100,
        "refund_status": "full", "status": "refunded"}))
    p = rec.adapter().get_payment(PAY)
    assert (p.amount_minor, p.amount_refunded_minor, p.refund_status) == (500100, 500100, "full")


# --------------------------------------------------------------- payment links
def test_payment_link_reference_fits_razorpays_limit_and_is_deterministic():
    ref = payment_link_reference(KEY)
    assert len(ref) == PAYMENT_LINK_REFERENCE_MAX == 40
    assert ref == payment_link_reference(KEY)

    rec = Recorder(lambda r: httpx.Response(200, json={
        "id": "plink_ERgihyaAAC0VNW", "amount": 100000, "amount_paid": 0,
        "status": "created", "short_url": "https://rzp.io/i/abc", "reference_id": ref}))
    link = rec.adapter().create_payment_link(
        merchant_id="M", customer_id="C", amount_minor=100000,
        source_payment_id="SYN_PAY_1", idempotency_key=KEY)
    sent = json.loads(rec.requests[0].content)
    assert len(sent["reference_id"]) <= 40, "Razorpay refuses a reference_id over 40 chars"
    assert sent["notes"]["idempotency_key"] == KEY
    assert link.id == "plink_ERgihyaAAC0VNW"


def test_payment_link_duplicate_reference_is_ambiguous_not_failed():
    """'already attempted' is the provider saying the first link exists."""
    rec = Recorder(lambda r: httpx.Response(400, json=_error(
        "BAD_REQUEST_ERROR", "payment link creation with reference ID already attempted")))
    with pytest.raises(ProviderAmbiguous):
        rec.adapter().create_payment_link(
            merchant_id="M", customer_id="C", amount_minor=100000,
            source_payment_id=None, idempotency_key=KEY)


def test_payment_link_5xx_is_ambiguous():
    rec = Recorder(lambda r: httpx.Response(502))
    with pytest.raises(ProviderAmbiguous):
        rec.adapter().create_payment_link(
            merchant_id="M", customer_id="C", amount_minor=1, source_payment_id=None,
            idempotency_key=KEY)


def test_find_payment_link_by_key_searches_by_reference_id():
    ref = payment_link_reference(KEY)

    def handler(r):
        assert r.url.path == "/v1/payment_links"
        assert r.url.params["reference_id"] == ref
        return httpx.Response(200, json={"payment_links": [{
            "id": "plink_FOUND", "amount": 100000, "amount_paid": 40000,
            "status": "partially_paid", "short_url": "u", "reference_id": ref}]})
    link = Recorder(handler).adapter().find_payment_link_by_idempotency_key(KEY)
    assert link.id == "plink_FOUND"
    assert (link.status, link.amount_paid_minor) == ("partially_paid", 40000)


def test_live_notification_fails_closed():
    rec = Recorder(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError) as e:
        rec.adapter().create_notification(merchant_id="M", customer_id="C",
                                          channel="email", template="t",
                                          idempotency_key=KEY)
    assert e.value.code == "INTEGRATION_UNAVAILABLE" and rec.requests == []
