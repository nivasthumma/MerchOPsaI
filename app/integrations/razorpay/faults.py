"""Deterministic fault injection — CONTRACT §35A (added by ADR-0008 #4).

§31 requires Failure/UNKNOWN scenarios and §33 requires an API-timeout case.
Those faults live in the ADAPTER, not the dataset: no amount of seeding
produces a provider timeout. This seam is inert unless a scenario enables it,
and every injected fault is recorded on the tool_call row so evaluation runs
stay auditable.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Fault(str, enum.Enum):
    TIMEOUT_BEFORE_SUBMIT = "TIMEOUT_BEFORE_SUBMIT"   # no action taken; safe to retry
    TIMEOUT_AFTER_SUBMIT = "TIMEOUT_AFTER_SUBMIT"     # may have happened => UNKNOWN
    CONNECTION_ERROR = "CONNECTION_ERROR"
    PROVIDER_5XX = "PROVIDER_5XX"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    SLOW_RESPONSE = "SLOW_RESPONSE"
    # Provider issues a refund id but the payment's amount_refunded never moves.
    # Exercises the verification predicate directly: trusting the API response
    # would report SUCCESS, reading the payment back reports PARTIAL.
    ACCEPTED_NOT_APPLIED = "ACCEPTED_NOT_APPLIED"


class ProviderTimeout(Exception):
    """Raised when the outcome of the call is genuinely unknown."""
    def __init__(self, msg: str, *, submitted: bool):
        super().__init__(msg)
        self.submitted = submitted


class ProviderAmbiguous(ProviderTimeout):
    """The request reached the provider and the response does not say what
    happened: a 5xx, a 409 while the same idempotency key is still in flight, a
    2xx body we cannot read, a duplicate-reference refusal.

    A subclass of ProviderTimeout on purpose. Every caller already routes a
    timeout to UNKNOWN and reconciliation, and that is the only honest reading
    of these too -- reporting FAILED would invite a retry of an operation that
    may already have moved money.
    """
    def __init__(self, msg: str, *, status_code: int | None = None):
        super().__init__(msg, submitted=True)
        self.status_code = status_code


class ProviderError(Exception):
    def __init__(self, msg: str, *, code: str = "EXTERNAL_API_ERROR"):
        super().__init__(msg)
        self.code = code


@dataclass
class FaultInjector:
    """Applies at most one fault, to one named operation."""
    fault: Fault | None = None
    on_operation: str = "create_refund"
    fired: bool = False
    # Set when a connectivity fault fires. A reconciliation lookup issued
    # during the SAME outage must also fail -- otherwise the timeout would be
    # instantly self-healing and UNKNOWN would never occur.
    down: bool = False

    @classmethod
    def disabled(cls) -> FaultInjector:
        return cls(fault=None)

    @classmethod
    def from_scenario(cls, cfg: dict | None) -> FaultInjector:
        if not cfg:
            return cls.disabled()
        raw = cfg.get("fault")
        if not raw:
            return cls.disabled()
        return cls(fault=Fault(raw), on_operation=cfg.get("on_operation", "create_refund"))

    def apply(self, operation: str) -> str | None:
        """Fire the fault if it targets this operation. Returns the fault name
        for the audit trail, or None. Raises for fault types that must
        interrupt the call."""
        if self.fault is None or self.fired or operation != self.on_operation:
            return None
        self.fired = True
        f = self.fault
        if f in (Fault.TIMEOUT_AFTER_SUBMIT, Fault.CONNECTION_ERROR,
                 Fault.TIMEOUT_BEFORE_SUBMIT):
            self.down = True

        if f is Fault.TIMEOUT_BEFORE_SUBMIT:
            raise ProviderTimeout(
                "Connection timed out before the request was submitted.", submitted=False)
        if f is Fault.TIMEOUT_AFTER_SUBMIT:
            raise ProviderTimeout(
                "Connection lost after the request was submitted. The provider may "
                "or may not have applied it.", submitted=True)
        if f is Fault.CONNECTION_ERROR:
            raise ProviderTimeout("Connection reset by peer.", submitted=True)
        if f is Fault.PROVIDER_5XX:
            raise ProviderError("Provider returned HTTP 503.", code="EXTERNAL_API_ERROR")
        if f is Fault.ACCEPTED_NOT_APPLIED:
            return f.value
        if f is Fault.MALFORMED_RESPONSE:
            return f.value      # adapter returns a deliberately malformed body
        if f is Fault.SLOW_RESPONSE:
            return f.value
        return f.value
