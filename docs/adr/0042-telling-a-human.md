# ADR-0042 — Telling a human

**Status:** Accepted · 2026-09-05
**Phase 1 of the readiness review, first item.**

## Context

Every control around the human was built. Approvals are created server-side,
carry an expiry, require two signatures at CRITICAL risk, and are re-checked
against policy at execution. Incidents open with a computed severity and a
revenue-at-risk figure derived from the affected payments. Actions that
reconciliation cannot settle land in an escalation queue whose own docstring
says it "must not be silently empty-looking". UNKNOWN is a first-class,
resolvable verification state.

And nothing in `app/` sent anything to anybody. No email, no SMS, no Slack, no
outbound webhook. Searching the tree for `smtp`, `sendgrid`, `twilio`, `slack`
or `pagerduty` returned nothing.

The only way to learn that a CRITICAL refund was waiting on your signature was
to have the page open at the moment it appeared. `approval_ttl_seconds`
defaults to **900** — fifteen minutes. Approvals also expire *lazily*: they stay
`PENDING` in the table until somebody tries to use one, so an approval that
lapsed and one that was never raised look identical from the database.

In production this does not degrade gracefully. It fails in one specific
direction: the approvals that expire unseen are the high-risk ones, because
those are exactly the ones policy refuses to auto-approve. The governance
controls were, in effect, a mechanism for reliably not doing the most valuable
recoveries.

## Decision

`app/notify`, in six pieces, each with one job.

**Routing is derived, never listed.** Who is told about an action is
`app.policy.engine.required_permissions(action_type)` — the same function the
policy engine gates on — intersected with the users of that merchant. A
recipient list maintained beside the permission model drifts from it, and it
drifts in one direction: somebody stays on the list who can no longer act, or
gains authority and stops being told. An action type the registry does not know
requires nothing, and "requires nothing" must not become "send it to everyone",
so an unknown type routes to nobody and says so.

**Both isolation boundaries are checked, outermost first.** A notification is
the one artefact that leaves the system and cannot be recalled. A cross-merchant
recipient is not a rendering bug, it is a breach delivered by email.

**Recorded before attempted.** A row is written and flushed before a socket
opens. A process killed mid-send leaves a `PENDING` row with an attempt counted
— a fact somebody can act on. Send-then-record produces a delivery with no
record, which is indistinguishable from one that never happened, which is the
problem this package exists to solve, rebuilt one level down.

**Deduplicated by a UNIQUE constraint.** `dedupe_key` is unique and the INSERT
is what decides; the preceding SELECT is only an optimisation. The sweep
recomputes "expiring soon" on every pass over a fifteen-minute window, and two
drains can run concurrently. This is the same argument `approval_signatures`
makes: under concurrency the constraint is the authority. It also makes the
cadence safe to tune — over-running costs queries and sends nothing twice, and a
sweep whose cost of over-running is a duplicate email is one nobody dares run
often enough.

**`log` is a real channel, and the default.** Not a no-op: it writes the
recipient, subject and full body as a structured line. A deployment with no SMTP
still answers "was the approver told, and what did it say?". Email, Slack and an
outbound signed webhook are configured or absent — and naming an unconfigured
channel in `NOTIFY_CHANNELS` raises at import rather than falling back, because
the failure mode of a silent fallback is somebody believing they are being
emailed.

**Three kinds arrive by event, two by sweep.** Approval requested, incident
opened at HIGH or above, and verification UNKNOWN are ordinary `app.events.bus`
consumers — so a notification commits with the write that caused it, a failing
consumer is retried three times and then visible as DEAD, and a slow channel
cannot hold up the request. An approval expiring and an action escalating have
no moment to hook: one is the absence of a decision, the other a threshold
crossed. Those are found by looking, on a cadence.

**Notifications are not §62 events.** §62 is a closed contract with the UI about
what to draw on a timeline. `NotificationKind` is a separate, shorter list of
things worth interrupting somebody for. `tool.started` belongs on a timeline and
nowhere near an inbox, and merging the two vocabularies would have forced one of
them to be wrong.

## Consequences

Three defects were found by building this, all pre-existing:

- `escalated_actions` never selected `action_type`. The operator work queue
  listed identifiers and amounts and left the reader to open each task to learn
  whether the stuck money was a refund going out or a payment link that may
  never have been sent. It is now selected, and declared on the response model.
- `incidents` has no `tenant_id` column — the merchant is the only link — so an
  early version of the incident consumer would have raised `AttributeError` on
  the first HIGH incident. The tenant is resolved from the merchant.
- Autogenerating this migration after `scripts/seed_data.py` produces an *empty*
  migration, because seed builds the schema with `create_all` from the same
  models autogenerate compares against. Noted in the migration itself, since it
  costs an hour to rediscover.

## What this does not do

**There is no scheduler, so the sweep needs a caller.** `scripts/notify_sweep.py`
drains the spine, sweeps, and retries what failed; `POST /notifications/sweep`
is the same thing over HTTP for a platform cron. Until something calls one of
them on a cadence, the two sweep-driven kinds do not fire — the same gap
detection and reconciliation already have, and the next item in Phase 1.

The cadence matters more here than elsewhere: the chase fires
`notify_approval_warning_seconds` (300) before an expiry that is 900 seconds
away, so a sweep running less often than every few minutes delivers the warning
after the window it was warning about has closed.

**Delivery is best-effort per attempt, durable per record.** Nothing here
retries inside a request. A channel that fails marks the row FAILED with the
error and `retry_pending` picks it up on the next sweep. `GET /notifications`
reports `undelivered` for exactly this reason: the number that matters is how
many people were not told.

**Quiet hours, per-user preferences, digests and escalation chains do not
exist.** `DeliveryRefused` is the seam they would attach to — a channel
declining on purpose records SUPPRESSED rather than FAILED, so a policy that
withholds a notification is already distinguishable from an outage.
