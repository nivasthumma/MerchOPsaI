"""Failure taxonomy and retry policy — MerchantOps §56, §57.

§56 lists eighteen categories and says every failure should carry:

    category · error_code · retryability · owning_subsystem
    evidence · correlation_id · recommended_next_action

The codes this system already raises are not those eighteen names. They were
chosen before §56 governed, they appear in scenario expectations, integration
tests, stored rows and the API's 409 bodies, and renaming them would be a large
diff whose only effect is to change strings. So they are MAPPED, the same way
ADR-0016 mapped the contract's section numbers rather than rewriting every
citation.

## Retryability is the interesting column

§57 is explicit that not every failure should be retried, and it names the ones
that must not be:

    authorization failure · policy denial · invalid payment
    invalid action · unknown financial state

and it gives UNKNOWN its own answer:

    UNKNOWN -> RECONCILE, never UNKNOWN -> blind retry

Writing that as data rather than prose is the point of this module. A caller
asking "may I try this again?" gets an answer from the table instead of from
whoever is reading the code that day, and `NEVER` on an authorization failure is
a fact about the system rather than a convention someone might not know.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Retryability(str, enum.Enum):
    """MerchantOps §57."""
    # Retrying changes nothing and may cause harm. Authorization, policy,
    # invalid input: the answer will be the same and the attempt is noise.
    NEVER = "NEVER"
    # Transient. Bounded exponential backoff with jitter, and a ceiling.
    BOUNDED_BACKOFF = "BOUNDED_BACKOFF"
    # The financial one. The outcome is unknown, so the correct move is to READ
    # provider state, never to repeat the action -- a blind retry here is the
    # single most dangerous thing this system could do.
    RECONCILE = "RECONCILE"
    # A human decides. Retrying is not wrong, it is not the question.
    ESCALATE = "ESCALATE"


class Subsystem(str, enum.Enum):
    """Who owns the failure — §56's `owning_subsystem`. The point is that an
    operator reading a failure knows which component to look at, so these are
    parts of this system rather than layers in the abstract."""
    TOOL_GATEWAY = "tool_gateway"
    POLICY = "policy_engine"
    APPROVAL = "approval_engine"
    PROVIDER = "provider_adapter"
    VERIFICATION = "verification_engine"
    RECONCILIATION = "reconciliation_engine"
    AGENT = "agent_runtime"
    DETECTION = "detection_engine"
    RECOVERY = "recovery_planner"
    WEBHOOK = "webhook_gateway"
    PLATFORM = "platform"


@dataclass(frozen=True)
class FailureClass:
    category: str                 # §56's own vocabulary
    retryability: Retryability
    owning_subsystem: Subsystem
    recommended_next_action: str

    def as_dict(self, error_code: str) -> dict:
        return {
            "error_code": error_code,
            "category": self.category,
            "retryability": self.retryability.value,
            "owning_subsystem": self.owning_subsystem.value,
            "recommended_next_action": self.recommended_next_action,
        }


_INTERNAL = FailureClass(
    "INTERNAL_ERROR", Retryability.ESCALATE, Subsystem.PLATFORM,
    "An unclassified failure. Treat as a defect in the taxonomy, not as a "
    "transient condition, and do not retry until it is classified.")

# error_code -> §56 category. Every code this system can raise appears here;
# `test_every_failure_code_is_classified` fails if one is added without a class.
TAXONOMY: dict[str, FailureClass] = {
    # --- input and authority: never retried (§57) ---
    "TOOL_INVALID_ARGUMENT": FailureClass(
        "INPUT_INVALID", Retryability.NEVER, Subsystem.TOOL_GATEWAY,
        "Correct the arguments. The same call will be rejected identically."),
    "AUTHORIZATION_DENIED": FailureClass(
        "AUTHORIZATION_FAILED", Retryability.NEVER, Subsystem.POLICY,
        "Obtain the required permission. Asking again does not grant it."),
    "POLICY_DENIED": FailureClass(
        "POLICY_DENIED", Retryability.NEVER, Subsystem.POLICY,
        "Policy refused this action. Change the action or the policy, not the "
        "number of attempts."),
    "APPROVAL_REJECTED": FailureClass(
        "APPROVAL_REJECTED", Retryability.NEVER, Subsystem.APPROVAL,
        "A human declined. Re-requesting without new evidence is a second ask, "
        "not a retry."),
    "APPROVAL_EXPIRED": FailureClass(
        "APPROVAL_REJECTED", Retryability.ESCALATE, Subsystem.APPROVAL,
        "The approval aged out before execution. Request a fresh decision."),
    "TOOL_UNAVAILABLE": FailureClass(
        "INPUT_INVALID", Retryability.NEVER, Subsystem.TOOL_GATEWAY,
        "The tool is not registered or has no executor. This is a build defect."),

    # --- transient: bounded backoff (§57) ---
    "TOOL_TIMEOUT": FailureClass(
        "AGENT_TIMEOUT", Retryability.BOUNDED_BACKOFF, Subsystem.TOOL_GATEWAY,
        "Retry with bounded exponential backoff and jitter."),
    "EXTERNAL_API_ERROR": FailureClass(
        "INTEGRATION_UNAVAILABLE", Retryability.BOUNDED_BACKOFF, Subsystem.PROVIDER,
        "Retry with bounded backoff. If it persists, escalate rather than "
        "continuing to call."),

    # --- the financial one (§57): reconcile, never retry ---
    "EXTERNAL_STATE_UNKNOWN": FailureClass(
        "UNKNOWN_EXTERNAL_STATE", Retryability.RECONCILE, Subsystem.RECONCILIATION,
        "Read provider state. Never re-issue the action: its outcome is unknown, "
        "which means it may already have happened."),
    "PARTIAL_EXECUTION": FailureClass(
        "EXECUTION_FAILED", Retryability.RECONCILE, Subsystem.VERIFICATION,
        "The provider accepted the action but the resulting state is incomplete. "
        "Read it back rather than repeating it."),
    "VERIFICATION_FAILED": FailureClass(
        "VERIFICATION_FAILED", Retryability.RECONCILE, Subsystem.VERIFICATION,
        "Independent read-back says the action did not take effect. Reconcile "
        "before concluding anything."),

    # --- agent (§56's AGENT_* family) ---
    "BUDGET_EXCEEDED": FailureClass(
        "AGENT_BUDGET_EXCEEDED", Retryability.ESCALATE, Subsystem.AGENT,
        "The run hit its bound. Narrow the task or raise the budget "
        "deliberately; do not simply run it again."),
    "MODEL_INVALID_OUTPUT": FailureClass(
        "AGENT_INVALID_OUTPUT", Retryability.BOUNDED_BACKOFF, Subsystem.AGENT,
        "The model's output did not match the schema. One re-ask is reasonable; "
        "a pattern of them is a prompt defect."),
    "AGENT_GROUNDING_FAILURE": FailureClass(
        "AGENT_GROUNDING_FAILURE", Retryability.ESCALATE, Subsystem.AGENT,
        "The model asserted something it cited no evidence for. Do not display "
        "the claim. A human should look at what it was reasoning from."),
    "EVIDENCE_INSUFFICIENT": FailureClass(
        "AGENT_GROUNDING_FAILURE", Retryability.ESCALATE, Subsystem.AGENT,
        "The evidence does not support a conclusion. Gather more or stop."),
    "REPLAY_DIVERGED": FailureClass(
        "INTERNAL_ERROR", Retryability.ESCALATE, Subsystem.AGENT,
        "A replay produced a different result from the recorded run. "
        "Investigate before trusting either."),

    # --- webhook gateway (§34) ---
    "WEBHOOK_INVALID": FailureClass(
        "WEBHOOK_INVALID", Retryability.NEVER, Subsystem.WEBHOOK,
        "The signature did not verify. Stored for investigation; never processed."),
    "WEBHOOK_DUPLICATE": FailureClass(
        "WEBHOOK_DUPLICATE", Retryability.NEVER, Subsystem.WEBHOOK,
        "Already delivered. Providers retry by design; this is the ordinary path."),

    # --- recovery (§28) ---
    "RECOVERY_STOPPED": FailureClass(
        "EXECUTION_FAILED", Retryability.ESCALATE, Subsystem.RECOVERY,
        "A stopping rule or a campaign bound refused the action. Lifting the "
        "bound is a decision, not a retry."),
    "RATE_LIMITED": FailureClass(
        "RATE_LIMITED", Retryability.BOUNDED_BACKOFF, Subsystem.PLATFORM,
        "Back off and try later. The limit is per window, not per caller mood."),
}


def classify(error_code: str | None) -> FailureClass:
    """Never returns None. An unclassified code is INTERNAL_ERROR and escalates:
    silently treating an unknown failure as retryable is how a permanent error
    becomes an infinite loop."""
    if not error_code:
        return _INTERNAL
    return TAXONOMY.get(error_code, _INTERNAL)


def describe(error_code: str | None, *, correlation_id: str | None = None,
             evidence: list | None = None) -> dict | None:
    """§56's full failure record, or None when nothing failed."""
    if not error_code:
        return None
    d = classify(error_code).as_dict(error_code)
    d["correlation_id"] = correlation_id
    d["evidence"] = evidence or []
    d["is_classified"] = error_code in TAXONOMY
    return d


def may_retry(error_code: str | None) -> bool:
    """The single question §57 exists to answer. `RECONCILE` is deliberately
    NOT a retry: it is a read."""
    return classify(error_code).retryability is Retryability.BOUNDED_BACKOFF
