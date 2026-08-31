"""Risk engine — MerchantOps §24.

Risk was previously a constant per tool. §24 says it is a function of what the
call is actually asking for: financial value, reversibility, uncertainty, bulk
size, customer impact.

## The floor rule

    final_risk = max(tool's declared class, computed risk)

Computed risk may **raise** a call above its tool's declared class. It may never
lower one below it. This is the single most important line in the module, and it
is not a style preference:

The declared class is a property of the operation and is fixed in the registry.
The computed part reads arguments — and arguments come from the model. If a
computed score could lower risk, then model-supplied input would have a path to
reduce a control, and an injected instruction that merely made a refund *look*
small would buy a weaker gate. Raising-only means the worst an attacker achieves
by manipulating the inputs is a stricter review than they wanted.

`tests/unit/test_risk.py` asserts the property directly, and a mutant that lets
the floor be lowered has to be caught.

## What reaches CRITICAL today, and what does not

§24's worked example is the calibration: a INR 5,000 refund is HIGH, a *bulk*
refund is CRITICAL. So transaction value alone never reaches CRITICAL here --
it tops out at HIGH, which for `request_refund` is already the declared floor.
Value is still assessed and recorded, because §26 requires the approver to see
the evidence behind the risk, and because the nine tools of §18 include
MEDIUM-floor actions where value genuinely changes the gate.

Two factors reach CRITICAL:

**uncertainty** -- a further action on a payment whose previous action never
settled. Not theoretical: the duplicate-action guard permits it, because an
UNKNOWN action is not one of the states it blocks on, and it is precisely the
path along which a double refund could occur.

**bulk_size** -- more than one financial action in one campaign, which is §24's
"Bulk refund -> CRITICAL". Passed in by the recovery planner; a caller acting on
a single payment leaves it None and it does not apply.

Number of affected users is still not computed. It is a customer-impact measure
rather than a breadth-of-action one, and nothing in the current interventions
distinguishes the two, so scoring it would be scoring `bulk_size` twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from app.config import get_settings
from app.models import RISK_ORDER, risk_at_least

# Financial value, as a fraction of the merchant's own refund limit. Relative
# rather than absolute: INR 5,000 is routine for one merchant and most of the
# ceiling for another, and a fixed rupee threshold would grade them identically.
#
# Value alone caps at HIGH, deliberately. MerchantOps §24's own worked example
# grades "Refund INR 5,000" -- which is exactly merchant A's limit -- as HIGH,
# and reserves CRITICAL for "Bulk refund". CRITICAL is about breadth, not the
# size of one transaction.
#
# The first draft of this module let value reach CRITICAL, which made the
# seeded duplicate refund (INR 4,999 against a INR 5,000 limit) require two
# approvers and broke nineteen tests. The tests were right: a single refund at
# the top of its permitted range is the most serious ordinary action, not an
# extraordinary one.
HIGH_FRACTION_OF_LIMIT = 0.40

# MerchantOps §24: "Bulk refund -> CRITICAL". Bulk is more than one financial
# action in one campaign -- the distinguishing feature is that a single mistake
# repeats itself, which is exactly what makes breadth its own risk dimension
# rather than a multiple of value.
#
# ADR-0019 deferred this factor because no tool took more than one target. The
# recovery planner is what creates multi-action campaigns, so it arrives here.
BULK_THRESHOLD = 2


@dataclass
class RiskFactor:
    name: str
    level: str
    reason: str


@dataclass
class RiskAssessment:
    level: str
    declared: str
    computed: str
    factors: list[RiskFactor] = field(default_factory=list)

    @property
    def was_raised(self) -> bool:
        return RISK_ORDER[self.level] > RISK_ORDER[self.declared]

    def as_dict(self) -> dict:
        return {
            "level": self.level, "declared": self.declared, "computed": self.computed,
            "raised": self.was_raised,
            "factors": [{"name": f.name, "level": f.level, "reason": f.reason}
                        for f in self.factors],
        }


def _merchant_refund_limit(session, merchant_id: str) -> int:
    cfg = session.execute(
        text("SELECT policy_config FROM merchants WHERE id = :m"), {"m": merchant_id}
    ).scalar() or {}
    return int(cfg.get("refund_limit_minor", get_settings().refund_amount_limit_minor))


def assess(session, *, tool_name: str, declared: str, merchant_id: str,
           arguments: dict, spec=None, bulk_size: int | None = None) -> RiskAssessment:
    """Grade one call. Reads only the registry, the session and the database —
    never model prose, and never a risk level supplied by the caller."""
    factors: list[RiskFactor] = []
    computed = "LOW"

    # ---- reversibility (§24) -------------------------------------------
    if spec is not None and not spec.reversible:
        factors.append(RiskFactor(
            "irreversibility", "MEDIUM",
            f"{tool_name} cannot be undone once it reaches the provider."))
        computed = risk_at_least(computed, "MEDIUM")

    # ---- financial value (§24) -----------------------------------------
    amount = arguments.get("amount_minor")
    if isinstance(amount, int) and amount > 0:
        limit = _merchant_refund_limit(session, merchant_id)
        fraction = amount / limit if limit else 1.0
        if fraction >= HIGH_FRACTION_OF_LIMIT:
            lvl, why = "HIGH", (
                f"{amount / 100:,.2f} is {fraction:.0%} of this merchant's "
                f"{limit / 100:,.2f} limit.")
        else:
            lvl, why = "MEDIUM", (
                f"{amount / 100:,.2f} is {fraction:.0%} of this merchant's limit.")
        factors.append(RiskFactor("financial_value", lvl, why))
        computed = risk_at_least(computed, lvl)

    # ---- bulk size (§24) -------------------------------------------------
    if bulk_size is not None and bulk_size >= BULK_THRESHOLD:
        factors.append(RiskFactor(
            "bulk_size", "CRITICAL",
            f"This action is one of {bulk_size} in a single campaign; a mistake "
            f"in it repeats across all of them."))
        computed = risk_at_least(computed, "CRITICAL")

    # ---- uncertainty (§24) ----------------------------------------------
    # Acting on a payment that already carries an unsettled action is riskier
    # than acting on a clean one: we do not yet know what the previous attempt
    # did, so this one cannot be reasoned about in isolation.
    target = arguments.get("synthetic_payment_id") or arguments.get("payment_id")
    if target:
        unsettled = session.execute(text("""
            SELECT count(*) FROM agent_actions
            WHERE merchant_id = :m AND target_payment_id = :p
              AND verification_state IN ('UNKNOWN', 'PARTIAL')
        """), {"m": merchant_id, "p": target}).scalar() or 0
        if unsettled:
            factors.append(RiskFactor(
                "uncertainty", "CRITICAL",
                f"{target} carries {unsettled} action(s) in an unsettled state; "
                f"the effect of a further action cannot be predicted."))
            computed = risk_at_least(computed, "CRITICAL")

    # ---- THE FLOOR RULE -------------------------------------------------
    # max(), never min(). See the module docstring.
    final = risk_at_least(declared, computed)
    return RiskAssessment(level=final, declared=declared, computed=computed,
                          factors=factors)
