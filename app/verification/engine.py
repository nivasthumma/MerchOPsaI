"""Verification engine — CONTRACT §25, §26 (predicate named by ADR-0008 #8).

The governing principle: a successful API response is not proof of business
state. Verification therefore re-reads the PAYMENT, not just the refund object.

A refund can legitimately sit at `pending`, so trusting `refund.status` alone
would report the ordinary path as ambiguous. `payment.amount_refunded` is the
business fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.integrations.razorpay.adapter import RazorpayAdapter
from app.integrations.razorpay.faults import ProviderError, ProviderTimeout
from app.models import VerificationState


@dataclass
class VerificationResult:
    state: VerificationState
    reason: str
    expected: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)
    external_reference: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state.value, "reason": self.reason,
            "expected": self.expected, "actual": self.actual,
            "external_reference": self.external_reference,
        }


def verify_refund(
    adapter: RazorpayAdapter,
    *,
    external_payment_id: str,
    expected_refund_minor: int,
    refunded_before_minor: int,
    external_reference: str | None,
) -> VerificationResult:
    """CONTRACT §25 verification predicate for a refund."""
    expected = {
        "external_payment_id": external_payment_id,
        "refund_amount_minor": expected_refund_minor,
        "amount_refunded_before_minor": refunded_before_minor,
        "amount_refunded_after_minor": refunded_before_minor + expected_refund_minor,
    }

    # --- read resulting state independently -------------------------------
    try:
        payment = adapter.get_payment(external_payment_id)
    except (ProviderTimeout, ProviderError) as e:
        # Could not read state at all => UNKNOWN. Never guess.
        return VerificationResult(
            VerificationState.UNKNOWN,
            f"Could not read resulting payment state: {e}. The final state of this "
            f"action is undetermined and must be resolved before it is trusted.",
            expected, {}, external_reference,
        )

    if payment is None:
        return VerificationResult(
            VerificationState.UNKNOWN,
            f"Payment {external_payment_id} could not be retrieved after the action.",
            expected, {}, external_reference,
        )

    refund = None
    if external_reference:
        try:
            refund = adapter.get_refund(external_reference)
        except (ProviderTimeout, ProviderError):
            refund = None

    actual = {
        "amount_refunded_minor": payment.amount_refunded_minor,
        "refund_status": payment.refund_status,
        "payment_status": payment.status,
        "refund_object_status": refund.status if refund else None,
    }

    delta = payment.amount_refunded_minor - refunded_before_minor

    # --- compare expected vs actual ---------------------------------------
    if delta >= expected_refund_minor and (refund is None or refund.status == "processed"):
        return VerificationResult(
            VerificationState.SUCCESS,
            f"Confirmed: amount_refunded increased by {delta} minor units "
            f"(expected {expected_refund_minor}) and the refund is processed.",
            expected, actual, external_reference,
        )

    if refund is not None and refund.status == "failed" and delta == 0:
        return VerificationResult(
            VerificationState.FAILED,
            "The provider reports the refund failed and the payment's refunded "
            "amount is unchanged.",
            expected, actual, external_reference,
        )

    if external_reference and (delta < expected_refund_minor):
        return VerificationResult(
            VerificationState.PARTIAL,
            f"The refund was accepted (reference {external_reference}) but the "
            f"payment reflects only {delta} of {expected_refund_minor} minor units. "
            f"The final state is incomplete.",
            expected, actual, external_reference,
        )

    if delta == 0 and external_reference is None:
        return VerificationResult(
            VerificationState.FAILED,
            "No external reference was issued and the payment is unchanged; the "
            "action did not take effect.",
            expected, actual, external_reference,
        )

    return VerificationResult(
        VerificationState.UNKNOWN,
        "Resulting state does not match any settled outcome; treating as UNKNOWN "
        "rather than guessing.",
        expected, actual, external_reference,
    )
