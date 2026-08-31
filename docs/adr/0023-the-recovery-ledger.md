# ADR 0023 — The recovery ledger, and two ways it was already lying

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §49, §50, §51

## Context

§49 ends with a sentence that is really a specification:

> The platform should never call the entire ₹4.72L "recovered."

Nothing in the build reported recovery at all, so nothing was violating it yet. Building the
report is what made the violations visible, and there were two.

## Decision

### 1. Six figures, one unit, and they nest

    revenue at risk  >=  recoverable  >=  attempted  >=  recovered + failed + unknown

The unit is **attributed exposure**: each charge counted only to the extent its incident is
responsible for it. Mixing gross charges into that chain is how a total ends up larger than
the thing it is a share of, and a merchant reads a recovery number that flatters the system.

`invariants_broken` is returned by the API and rendered by the UI rather than raised. A
violated ordering is a reporting defect that has to be visible, and a dashboard that refuses
to draw is one nobody can use to find out why.

### 2. A payment link that was sent is not money recovered

The first defect, and the one §49 names.

`settle_plan` mapped any verified SUCCESS to RECOVERED with the action's full amount. For a
refund that is right: SUCCESS means the money went back. For a payment link SUCCESS means
**a link now exists** — no customer has paid anything. Dispatching one and verifying it
reported the entire charge as recovered.

It was correct when written, because REFUND was the only executable intervention. Phase 5
made PAYMENT_LINK executable and did not revisit it. Settlement is now per intervention:

    refund        SUCCESS = the money went back      -> RECOVERED
    payment link  SUCCESS = a link exists            -> ATTEMPTED
                  provider says the link is PAID     -> RECOVERED
    notification  SUCCESS = a message was sent       -> ATTEMPTED

A paid link is counted at its **attributed** share, not the gross charge: only that part was
ever at risk from this incident.

### 3. Every intervention was being dispatched as a refund

The second defect, found by the same work.

`dispatch_candidate` built one hardcoded request string: `"Refund payment X amount N…"`.
Again correct while REFUND was alone, and again not revisited. A payment-link candidate was
dispatched as a refund request, which policy then refused because a failed payment is not
refundable.

It failed safe and it failed for the wrong reason. A recovery that never happens because the
system asked the wrong question is still a recovery that never happens, and the refusal
message pointed at refundability rather than at the mistake. The request is now built from
the candidate's intervention, and an intervention with no dispatch form is refused by name.

Both defects share a shape worth naming: **a mapping that was total when written and became
partial when a case was added.** Neither had a test, because when they were written there was
nothing to distinguish.

### 4. Shares are allocated exactly, not rounded independently

ADR-0020 fixed a paise-level drift by rounding the plan's aggregate once. The ledger needs
the *parts* too — attempted recovery is a sum over the candidates actually dispatched — so
per-candidate shares are allocated with the residual placed on the largest, and the parts sum
to the whole by construction. A test asserts it rather than trusting it.

### 5. `unknown` is its own bucket

Not folded into failed, not folded into recovered. It is the honest size of what the system
does not know, and §33 exists to keep that visible. A mutant that folds it into recovered has
to be caught, and `LDG-04` catches it.

### 6. The dashboard shows a chain, not a row of tiles

§49's misreading is a layout as much as a number. Four equal tiles let a reader take the
first figure as the headline and the last as a footnote, which is exactly backwards. The
ledger renders as an ordered list that narrows, each figure a subset of the one above, with
the basis stated underneath rather than assumed.

`/dashboard` is deliberately a different endpoint from `/metrics`. One reports money, the
other counts operations, and merging them is how "12" comes to mean tasks on one row and
rupees on the next.

### 7. The incident page shows untrusted evidence as untrusted

§51 asks for evidence on the incident page. Some of that evidence is merchant free text, and
the backend already tags it. The page renders the tag — quarantined, visibly, but not hidden:
an operator needs to see what the record actually contains, and stripping the flag would push
the judgement onto whoever reads it.

Expected recovery never appears without its basis, on this page or anywhere else.

## Consequences

- `recovery_candidates` gains `attributed_amount_minor` so the ledger has one unit.
- A payment link's conversion is now trackable in principle but is only observed when
  something reads the link's state back — `settle_plan` does it on demand, and no webhook
  subscribes to `payment_link.paid` yet. Until one does, a paid link is discovered only when
  a plan is settled.
- The SPA gains `/dashboard` and `/incidents/:id`. 168 web tests.
- 272 backend tests, 154 scenarios, 50 mutants.
