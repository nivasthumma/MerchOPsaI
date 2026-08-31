# ADR 0019 — Computed risk, the floor rule, and a second pair of eyes

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §24, §25, §26

## Context

Risk was a constant per tool: `request_refund` is HIGH, everything else is LOW, declared in
a dict in the policy engine. §24 says risk is a function of what the call is actually
asking for — financial value, reversibility, uncertainty, bulk size, customer impact — and
§25 lists a `REQUIRE_DUAL_APPROVAL` outcome that had no implementation.

## Decision

### 1. The floor rule

    final_risk = max(tool's declared class, computed risk)

Computed risk may **raise** a call above its tool's declared class. It may never lower one.

This is not a style preference. The declared class is a property of the operation, fixed in
the registry. The computed part reads *arguments*, and arguments come from the model. If a
computed score could lower risk, model-supplied input would have a path to weaken a
control: an injected instruction that merely made an action *look* small would buy a softer
gate. Raising-only means the worst an attacker achieves by manipulating inputs is a
stricter review than they wanted.

`RISK_ORDER` is ordinal rather than alphabetical, because compared as strings
`"CRITICAL" < "HIGH"` and the rule silently inverts. There is a unit test asserting exactly
that trap.

Two mutants cover the rule from both sides — one that lets computed risk replace the floor,
one that never raises above it — and both are caught by graded scenarios (RSK-07 and
RSK-02 respectively). RSK-07 exists specifically because no pre-existing scenario could
distinguish a floor from a starting guess.

### 2. Value alone never reaches CRITICAL

The first implementation let a refund at ≥80% of the merchant's limit grade CRITICAL. That
made the seeded duplicate refund — INR 4,999 against a INR 5,000 limit — require two
approvers, and broke nineteen tests.

The tests were right and the spec settles it: §24's worked example grades "Refund ₹5,000"
as HIGH and reserves CRITICAL for "Bulk refund". CRITICAL is about **breadth**, not the
size of one transaction. A single refund at the top of its permitted range is the most
serious *ordinary* action, not an extraordinary one.

Value is still computed and recorded, for two reasons: §26 requires the approver to see the
evidence behind the risk, and §18's remaining nine tools include MEDIUM-floor actions where
value genuinely changes the gate.

### 3. What does reach CRITICAL today

One factor: a further action on a payment whose previous action never settled.

That is not theoretical. The duplicate-action guard blocks on `PENDING`, `SUBMITTED` and
`CONFIRMED` — an `UNKNOWN` action is none of those, so a second refund is permitted, and it
is precisely the path along which a double refund could occur. Two pairs of eyes is the
right answer to "we do not know what the last attempt did".

It is also narrower than it first appears, which is worth recording: a timed-out **full**
refund leaves no refundable balance, so the balance check denies the second attempt before
risk is ever graded. The factor only bites on a **partial** unsettled refund — where money
is still on the table *and* the earlier attempt is unresolved. The test and scenario setup
both use `SYN_PAY_0007` for that reason.

Bulk size and affected-user count are not computed. No tool takes more than one target, so
there is nothing to count; they arrive with the recovery planner (§23), which is what
creates bulk actions in the first place.

### 4. Dual approval is a UNIQUE constraint, not an if-statement

`approval_signatures` carries `UNIQUE(approval_id, user_id)`.

"Two approvers" enforced by application logic is a check a retry, a race, or a later
refactor can get past — and check-then-insert is a race that two clicks from one user can
win. Enforced by the database, one person signing twice is not a case the application has
to remember to reject; it is a write that cannot succeed.

The rest follows:

- One signature records and returns; nothing external is touched, and the task stays
  `AWAITING_APPROVAL`.
- The second signer still passes every gate. A second pair of eyes is not a bypass — an
  analyst without `action:refund` can sign and still cannot be the signature that executes.
- **One veto is enough.** Two people to say yes, one to say no. Requiring consensus to
  *stop* would make the extra approver a weaker control than a single one.
- `required_signatures` is stored on the approval at proposal time. A later policy change
  must not quietly reduce what an in-flight action needs.

### 5. The registry is now the only declaration of risk and permissions

`TOOL_RISK` and `TOOL_PERMISSIONS` in `app/policy/engine.py` duplicated what `ToolSpec`
already declared, and the engine's copy silently won: the runtime passed
`spec.risk_class.value` into `PolicyContext` and `evaluate()` overwrote it.

They agreed, so nothing was broken. But a tool added to the registry and forgotten in the
dict was denied as unregistered — fail-closed, so not a live vulnerability, and a trap all
the same. §18 adds nine tools; the duplication is retired before they arrive rather than
after. A test asserts the dicts are gone and that the accessors agree with the registry for
every tool.

### 6. A second approver had to be seeded

Each merchant had exactly one user with `action:refund`, so dual approval could only ever
have been demonstrated by the same person signing twice — the exact thing the control
forbids. `USR_A_APPROVER` is added to the literal user list, which consumes no RNG and
leaves the rest of the dataset byte-identical.

## Consequences

- `RiskLevel` and `RiskClass` gain `CRITICAL`; `Decision` gains `REQUIRE_DUAL_APPROVAL`.
- Every approval now carries its risk assessment — declared, computed, whether it was
  raised, and the factors with their reasons — into the audit trail and the approval
  record. That is §26's evidence package for the human, and it is what a UI needs to say
  *why* two signatures are being asked for.
- The value factor changes no outcome today, because `request_refund` is the only
  non-read tool and its floor is already HIGH. It is not dead code — it is recorded
  evidence and it becomes load-bearing in §18 — but it should not be described as an
  active control until then.
- 172 tests, 127 scenarios, 28 mutants.
