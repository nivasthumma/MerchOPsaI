# ADR 0018 — A webhook is evidence, not authority

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §11, §32, §34, §35, §54

## Context

Reconciliation could only poll. An action left `UNKNOWN` sat until a sweep noticed it, so
settlement ran at sweep cadence and MerchantOps §34's pipeline — signature, deduplication,
durable storage, reconciliation — did not exist.

The obvious implementation is also the dangerous one: take `refund.processed`, write
`verification_state = SUCCESS`, done. That would hand an unauthenticated HTTP endpoint the
power to declare that money moved.

## Decision

### 1. The webhook decides *when* to look, never *what was found*

Nothing in `app/webhooks/` writes `agent_actions.verification_state` from a payload. A
verified, subscribed event finds the actions touching that entity and calls
`reverify_action`, which reads provider state **through the adapter**. The payload's own
`status` field is stored and never consulted.

MerchantOps §32 already establishes that a provider's HTTP 200 is not proof of business
state. A pushed message is weaker evidence than a response to a request we made, not
stronger, so it gets the same treatment: independent read-back.

This is what makes forgery uninteresting rather than catastrophic. An attacker who defeats
the signature can make the system re-read state it would have read anyway. They cannot
tell it what that state is. The scenario `WHK-04` delivers a payload claiming
`refund.processed` against provider state that says otherwise, and asserts the system
reports what it read.

### 2. Rejected deliveries are stored anyway

A delivery refused for a bad signature that leaves no row is an attack nobody can
investigate. Every delivery lands in `webhook_events` with the status it earned —
`INVALID`, `DUPLICATE`, `IGNORED`, `PROCESSED` — along with the hash of the exact bytes the
signature was computed over.

### 3. No secret configured means store-but-never-act

Three options were available when `RAZORPAY_WEBHOOK_SECRET` is unset: refuse everything,
accept everything, or record without acting.

Refusing everything makes the endpoint untestable in a build that has no Razorpay
credentials at all. Accepting everything is a forgery hole that looks like a working
feature. So: the delivery is stored with `signature_valid=False` and status `IGNORED`, and
processing never runs. `/health` publishes `webhook_signature_verification` so that "no
state changed" is never ambiguous between "no events arrived" and "events arrived
unverified".

### 4. The merchant is resolved from our records, not the payload

Razorpay's envelope carries an `account_id`. It is ignored. Which merchant an entity
belongs to is a fact this system already holds, and reading it from the body would let a
forged delivery address another tenant — the one thing §54 exists to prevent. Resolution
goes through `payments.external_payment_id` and `agent_actions`, and an entity we cannot
place leaves `merchant_id` NULL rather than being guessed at.

Unattributed events are consequently visible only as a count on `GET /webhooks/events`.
Showing their bodies to whoever asked first would turn an unauthenticated write into a
cross-tenant read.

### 5. The response is 200 once the delivery is stored, including for a bad signature

A provider that receives a non-2xx retries. Retrying a forged or malformed delivery
achieves nothing but load. The outcome is in the response body and in the event store; the
status code reports whether we accepted custody of the message, not whether we believed it.

### 6. Deduplication is a constraint, not a check

`webhook_events.event_id` is UNIQUE and the insert is attempted rather than pre-checked. A
SELECT-then-INSERT is a race that two concurrent deliveries both lose, and providers retry
by design, so the collision path is ordinary rather than exceptional. Where the provider
sends no event-id header the payload hash stands in, which treats two byte-identical
deliveries as one — the desired behaviour, and stated in the stored note rather than
hidden.

### 7. A contradiction becomes a CRITICAL incident, never a silent correction

MerchantOps §35: internal `SUCCESS`, provider `FAILED` is a reconciliation incident. When
re-verification regresses an action away from `SUCCESS`, `RECONCILIATION_MISMATCH` is
raised at `CRITICAL` — there is no small version of "a financial claim we already made is
in doubt" — and **no correction is applied**. Overwriting our record would erase the only
evidence that the two ever disagreed.

`UNKNOWN` is deliberately excluded from the contradiction set. That is a failure to *read*
provider state, not a disagreement about what it is, and treating it as a mismatch would
turn every provider blip into a CRITICAL false alarm. It stays with the reconciliation
sweep, which is built for exactly that ambiguity.

The incident's `detection_key` is derived from `(action, contradicted state)`, so a
provider redelivering the same event fifty times produces one incident.

## Consequences

- Settlement is now event-driven as well as swept. The reconciliation sweep remains as the
  backstop for actions no webhook ever arrives for — README limitation #4 is narrowed,
  not closed, and still says so.
- `webhook_events` is MerchantOps §11's durable event store. ADR-0017 §1 deferred it to
  this phase on the grounds that a table with no writer is a skeleton component; it now
  has one. It stores provider-delivered events only — internal event sourcing is still not
  a thing this system does.
- The webhook endpoint is the only unauthenticated write in the application. It carries
  its own rate-limit class, because the limiter keys on an identity and there is no
  identity here.
- 24 mutants, four of them new: accept any signature, stop deduplicating, process a
  delivery that failed its signature, and stop treating a contradiction as a mismatch. All
  four are caught by a graded scenario rather than by unit tests alone.
