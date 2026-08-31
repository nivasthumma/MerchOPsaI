# ADR 0021 — The fifteen tools, and the boundary between reading and acting

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §18, §24, §29, §31, §32, §39, §55

## Context

§18 specifies fifteen tools. Six existed. The nine missing ones included two that reach
outside the system — `generate_payment_link` and `send_customer_notification` — and those
two are the reason this phase needed an ADR rather than nine similar files.

## Decision

### 1. Every tool is a read or an approved action, never both and never neither

    read    -> executes on request, LOW risk, reversible, no external effect
    action  -> approval, idempotency key, action record, verification

Both failure modes are real and one of them was already present.

**Neither** is what `get_refund_status` had been for four phases: registered, visible to
the model, authorised by the policy engine, and returning `TOOL_UNAVAILABLE` because
nothing implemented it. A registered tool with no implementation is a trap — everything
upstream says yes and only the executor knows the truth. It is implemented now, and a test
asserts the partition holds for all fifteen.

**Both** would be worse: a state-changing action with a direct route that skips approval.
`generate_payment_link` and `send_customer_notification` take the same path a refund takes,
because a notification cannot be unsent and a payment link, once a customer has it, has
been given to them. Routing either through `execute_read_tool` would have lost the action
record, the idempotency key and the read-back in one move. A mutant that adds a contacting
action to the read path has to be caught.

### 2. The model chooses which, never how much or what to say

Two injection sinks, closed the same way.

`generate_payment_link` takes **no amount**. The figure comes from the failed payment's own
row at execution time. A model-supplied amount is a model-supplied request for money, and
the fact that an injected instruction would then have to pass a policy limit is not a
reason to accept one.

`send_customer_notification` takes a **template from a fixed enum**, not a body. The model
decides which approved message fits; it does not compose text that reaches a customer. Free
text on that path is text nobody reviewed.

### 3. Opt-out is re-checked at execution, not only in planning

§28's opt-out is a property of the customer. `execute_notification` re-reads it immediately
before sending, so a human approving a stale recommendation cannot override it either. The
planner's check is a courtesy that keeps ineligible candidates out of the queue; this one
is the control.

### 4. Reading provider state is a different act from reading ours

`get_payment` reads our record. `get_payment_status` reads the provider's and returns both
alongside `internal_and_provider_agree`. When the provider read fails, the tool reports a
failed read — it does not fall back to internal state, because answering a question about
external state with internal state is the substitution §32 exists to prevent.

`reconcile_transaction` is the only read tool that writes, and what it writes is the result
of a read: it calls the same `reverify_action` the sweep and the webhook path use. It
cannot create a financial effect, which is why it is LOW risk despite changing state — the
same reasoning that lets `/tasks/{id}/reverify` run without approval.

### 5. `action:recover` is separate from `action:refund`

Contacting a customer and moving money back to them are different authorities (§55). An
analyst has neither; an owner has both. Splitting them costs nothing now and is the
difference between a support role that can chase a failed payment and one that can issue
refunds.

### 6. MEDIUM means a human, and a campaign of MEDIUMs means escalation

§24 grades a notification MEDIUM, and under §25 anything above LOW requires approval. So
every payment link and every message is signed off individually. That is strict, and
deliberate: no money moves, but a third party is contacted, and this system's posture is
that such effects get a human.

The consequence is the interesting part. `PAYMENT_LINK` becoming executable turned the
degradation plan's thirty-three advisory candidates into thirty-three actionable ones,
which is bulk, which is CRITICAL (§24), which is above §28's ceiling for unattended
recovery. The campaign now escalates rather than running — reachable from the seeded
dataset rather than from state a test had to plant, closing the limitation ADR-0020 was
explicit about.

### 7. A policy control was firing for the wrong reason

Found by a test on a new tool.

The refund amount limit, the refundable-balance check and the payment-not-failed check ran
for **any** tool whose arguments carried `amount_minor`, despite a comment saying "HIGH risk
only". With one money-shaped tool that was invisible. With two, a payment link carrying an
amount was measured against the merchant's *refund* limit and refused with a message about
refunds.

The denial was convenient and the reasoning was wrong, and a limit that fires for the wrong
reason is a limit nobody can predict. Those checks are now scoped to `request_refund`; the
generic "an amount must be a positive integer" still applies to any tool that names one.

### 8. The planner had to be extended, and that is a hazard in itself

The deterministic planner is what the entire evaluation suite measures through, so a broad
new trigger silently rewrites what a hundred existing scenarios test. Every new branch is
gated narrowly, and one still went wrong: a request naming an incident id was pulled into a
read, which broke the recovery planner's own dispatch ("Refund payment X … for incident
INC_Y"). A request that asks for an action is never a lookup, whatever entities it mentions.
Four tests caught it; no scenario did, because no scenario names an incident id.

## Consequences

- 15 tools. `RETRY` and `SUBSCRIPTION_RETRY` remain unimplemented interventions —
  `CUSTOMER_NOTIFICATION` now has a tool but is not planned as a standalone intervention,
  because no incident type maps to it: it is something reached for alongside a recovery,
  not a recovery in itself.
- `create_notification` fails closed on the live adapter. Razorpay notifies *about a
  payment link*; it is not a messaging service, and this build has no email or SMS
  provider. Fabricating a success there would be the one thing the verification design
  exists to prevent — reporting that a customer was contacted when nobody was.
- Six of the seven new mutants are caught by unit tests only. The scenario suite cannot
  reach most of these paths because the deterministic planner does not compose customer
  contact on its own, and giving it the freedom to do so would be the wrong fix.
- One scenario in this phase was **vacuous when written**: TOOL-05 asserted that an
  unauthorised analyst did not reach `generate_payment_link`, at a point when the planner
  never called it for anyone. Dropping the permission entirely changed nothing and the
  mutation harness said so. It now names a payment, the planner proposes the tool, and
  policy is what refuses.
