"""What produced a run, recorded as it happened — AI mode and version governance.

## AI mode

Every run records exactly one of:

    AI_SUCCESS               the configured model produced every turn
    AI_FAILED_FALLBACK       the model was reached and then failed mid-run; the
                             deterministic planner finished the run
    AI_UNAVAILABLE_FALLBACK  a model was configured and could not be reached at
                             all; the deterministic planner ran the whole run
    DETERMINISTIC_ONLY       no model was configured; the planner ran by design

A fallback run is never presented as a model result. The planner is not a
model and does not reason (app/llm/deterministic.py); a run it finished must
say so on the task, in the API and on the screen, or a merchant reads
arithmetic as judgement.

## Versions

`configuration_version` hashes the settings that govern what a run is allowed
to do. It is derived, not hand-kept, so it cannot be stale. Versions this
system does not have are not invented: there is no retrieval component, so no
`retrieval_version` is recorded, and `dataset_version` belongs to evaluation
reports, not to production runs.
"""
from __future__ import annotations

import enum
import hashlib
import json


class AIMode(str, enum.Enum):
    AI_SUCCESS = "AI_SUCCESS"
    AI_FAILED_FALLBACK = "AI_FAILED_FALLBACK"
    AI_UNAVAILABLE_FALLBACK = "AI_UNAVAILABLE_FALLBACK"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"

    @property
    def is_fallback(self) -> bool:
        return self in (AIMode.AI_FAILED_FALLBACK, AIMode.AI_UNAVAILABLE_FALLBACK)


def initial_mode(provider_name: str) -> AIMode:
    return AIMode.DETERMINISTIC_ONLY if provider_name == "deterministic" else AIMode.AI_SUCCESS


#: The settings that bound what a run may do or which model it uses. Changing
#: any of them changes the hash; nothing else does.
_GOVERNING = (
    "max_tool_calls_per_task", "max_llm_turns_per_task", "effective_wall_clock_seconds",
    "refund_amount_limit_minor", "approval_ttl_seconds",
    "recovery_max_amount_minor", "recovery_max_actions",
    "recovery_max_attempts_per_customer", "recovery_min_expected_minor",
    "resolved_llm_provider", "llm_model", "llm_effort", "llm_fallback_enabled",
    "resolved_razorpay_mode", "workflow_version",
)


def configuration_version(settings) -> str:
    values = {k: getattr(settings, k, None) for k in _GOVERNING}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode())
    return "cfg-" + digest.hexdigest()[:16]


def policy_input_hash(*, action_type: str, arguments: dict, merchant_id: str,
                      risk_level: str) -> str:
    """What an approval was granted for, as a hash (app/agent/approval.py).

    Execution recomputes it from the approval's stored payload and refuses on a
    mismatch: a human approved THIS request, and a payload changed after the
    fact is a different request nobody approved.
    """
    canonical = json.dumps({"action": action_type, "arguments": arguments,
                            "merchant": merchant_id, "risk": risk_level},
                           sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
