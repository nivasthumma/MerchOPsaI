# ADR 0040 — The merchant digital twin is a projection, not a second copy

**Status:** Proposed · 2026-09-02 · *design only; nothing implemented yet*

## Context

MerchantOps v2 §14 asks for a continuously updated representation of merchant
operational health:

```text
MerchantState
├── Financial            Revenue · GMV · Refunds · Revenue At Risk
├── Payments             Success Rate · Failure Rate · Latency · Method Health
├── Customers            Active · Affected · Recovery Candidates
├── Incidents
├── Recovery
└── Operational Health

The dashboard reads this state.
The AI receives relevant portions of it.
```

Read against the code, almost all of this already exists — and none of it exists
*together*.

| §14 branch | where it is computed today |
|---|---|
| Financial → Revenue | `get_revenue_summary` (a tool, per call) |
| Financial → Revenue At Risk | `RecoveryLedger.at_risk_minor` |
| Financial → Refunds | `refunds`, and the ledger's settled figures |
| Financial → **GMV** | **nowhere** — revenue is captured value; GMV is attempted |
| Payments → Success / Failure Rate | `get_payment_metrics` |
| Payments → Method Health | `get_payment_metrics(method=…)` |
| Payments → **Latency** | **not recorded** — see below |
| Customers → Active | `customers` |
| Customers → Affected, Recovery Candidates | `recovery_candidates` |
| Incidents | `ledger.dashboard()["incidents"]` |
| Recovery | `build_ledger` |
| Operational Health | `operational_metrics`, `objectives` |

So §14 is not six new subsystems. It is **one coherent object assembled from
parts that already exist and have never been assembled**, plus the thing the
last line of §14 asks for and nothing currently provides: handing the agent a
*relevant portion* rather than everything.

## Decision (proposed)

### 1. `MerchantState` is computed, not stored

No `merchant_state` table. The figures are derived from rows that change
underneath them, and a cached count is a count that disagrees with its rows the
first time a candidate moves — the argument `app/recovery/campaign.py` already
makes for the campaign card, and ADR-0037's reason for not adding a `campaigns`
table beside `recovery_plans`.

§14's "continuously updated" is satisfied by *being* the rows rather than by a
refresh loop chasing them. A projection is always current by construction; a
stored twin is current only as often as somebody remembered to invalidate it.

**If caching is ever needed** — and at 590 payments it is not — the seam is
`build_state(session, merchant_id)` returning a `MerchantState`. A cache goes
behind that function without any caller learning about it. Deciding to cache
before there is a measurement that demands it would buy staleness for nothing.

### 2. The agent gets a projection of it, not the object

§14's last line and §26 are the same instruction: "Do not send the entire
database to the LLM." So the twin exposes:

```python
build_state(session, merchant_id) -> MerchantState        # the dashboard's view
MerchantState.for_incident(incident) -> dict              # the agent's slice
```

`for_incident` narrows to what bears on *this* incident — the affected method's
health rather than every method's, the incident's own exposure rather than the
merchant's whole ledger. A twin that is handed to the model whole is a context
bill, not a context strategy.

The slice is **facts only**, carrying the same FACT/INFERENCE separation
`build_investigation_request` already uses: the model is not asked to re-derive
figures the calculation engine owns (§22), and is not free to contradict them.

### 3. Two branches report as unmeasurable rather than being invented

**Payments → Latency is not recorded.** `agent_actions` carries
`provider_latency_ms` and `verification_latency_ms`, but those are *our* call to
Razorpay. §14 means how long a customer's payment took at the rail, and
`payments` has no such column: there is a `created_at` and nothing to subtract
from it. Adding one means the ingestion path capturing it, which is a change to
what is collected rather than to what is reported.

**GMV is computable but is not revenue.** GMV is attempted value, revenue is
captured value, and the ratio between them is the conversion story §14 puts them
side by side to tell. It is a new figure; it is arithmetic over `payments`, and
it belongs in the Financial branch with its definition attached so nobody reads
it as a bigger revenue number.

Both follow the precedent `app/metrics.py` set and ADR-0034 repeated: a figure
computed from nothing is worse than a blank, because the blank prompts the
question and the number closes it. `MerchantState` marks a branch
`measured=False` with a reason rather than reporting a zero.

## What this does not do

- **No new storage.** Nothing is written; the twin has no migration.
- **No new numbers except GMV.** Everything else already has an owner, and
  recomputing a figure beside its owner is how two answers to one question get
  created. `MerchantState` *calls* `build_ledger`, `operational_metrics` and the
  investigation queries rather than reimplementing them.
- **No twin for the model to write to.** The agent reads a slice. Nothing in
  §14 suggests the model should update merchant state, and everything in §5 and
  §89 says it must not.

## Open questions to settle before implementing

1. **Does the dashboard endpoint become the twin, or read it?**
   `ledger.dashboard()` already returns recovery + incidents + agent activity.
   Making `MerchantState` a superset and having `dashboard()` return a slice of
   it avoids two assemblers; it also changes an endpoint the SPA consumes, so it
   wants an OpenAPI diff and a look at `web/src/api/types.ts`.

2. **Is `for_incident` a method or a separate context module?**
   `app/agent/` already owns context construction. If the slice grows past a
   handful of fields it belongs beside `build_investigation_request`, not on the
   state object.

3. **What is the twin's freshness contract?**
   Computed-per-read means "as of this request". That is worth *saying* in the
   response — a dashboard figure with no as-of is a figure somebody will quote
   an hour later.

## Consequences if adopted

- One place to ask "how is this merchant doing", instead of four.
- The agent's context stops being assembled ad hoc per call site.
- Two honest blanks (latency, and any branch with no data) appear on the
  dashboard, which is the point: §14's tree is a claim about what the platform
  can see, and the gaps in it are information.
- The mutation surface grows by whatever guards the `measured=False` reporting,
  since "invent a number for an unmeasurable branch" is precisely the mutant
  that must not survive — `metrics.py` already carries one of exactly that shape.
