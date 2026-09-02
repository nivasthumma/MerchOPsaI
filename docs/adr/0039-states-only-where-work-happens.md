# ADR 0039 — The incident machine gains only states that work actually enters

**Status:** Accepted · 2026-09-02

## Context

MerchantOps v2 §20 lists seventeen canonical incident states against the ten
this build had. The obvious reading is "add seven enum members", and it is the
wrong one.

A state nothing ever transitions through is worse than a smaller honest
machine. No scenario can grade it. No mutant can break it. A merchant reading a
status list finds half the entries unreachable, and a reviewer auditing the
lifecycle cannot tell which states describe the system and which describe an
aspiration.

The build already had this problem and nobody had noticed: **`EXECUTING` and
`VERIFYING` shipped in v1's enum and nothing ever entered them.** The execution
path moved `agent_actions` and left the incident wherever the investigation had
put it. Two of the ten states were decoration.

## Decision

Five states are added, each entered by code that runs. Two are refused because
they belong to a different entity, and one because it has no moment here.

### Added

| state | the moment it describes | who moves it |
|---|---|---|
| `EVIDENCE_COLLECTING` | the first tool call — evidence is arriving | `AgentRuntime` → `on_phase` |
| `DIAGNOSING` | an output block parsed; weighing what was gathered | `AgentRuntime` → `on_phase` |
| `APPROVED` | a human said yes; nothing executed yet | `approve_and_execute` |
| `RECONCILING` | verification could not settle it; the sweep owns it | `approve_and_execute` |
| `MEASURING` | actions settled; the ledger totals what it was worth | `approve_and_execute` |

Closing the pre-existing gap turned out to be most of the work. `plan_recovery`
recorded a `recovery_planned` event without moving the incident, and
`dispatch_candidate` ran a task through policy without the incident saying so —
so `RECOVERY_PLANNED`, `POLICY_EVALUATING`, `APPROVAL_REQUIRED`, `EXECUTING` and
`VERIFYING` are now entered too.

### Refused

- **`RECEIVED`, `VALIDATING`** — the *event's* lifecycle, and they already exist
  on `webhook_events`: `WebhookStatus` is
  `RECEIVED · PROCESSED · IGNORED · DUPLICATE · INVALID`, with signature
  validation at `app/webhooks/razorpay.py:171` producing `INVALID`. An incident
  does not exist before detection creates it; there is no moment at which one is
  "received". Adding them would model one entity's lifecycle on another — the
  mistake ADR-0037 avoided by not putting a `campaigns` table beside
  `recovery_plans`.
- **`IMPACT_CALCULATING`** — revenue-at-risk is computed *by detection*, before
  the incident row exists. There is nothing to move.

### Not renamed

`TRIAGING`, `RECOVERY_PLANNING` and `DIAGNOSING`-for-`ROOT_CAUSE_IDENTIFIED` are
not adopted as renames. ADR-0016 settled that renaming for its own sake is "a
large diff whose only effect is to change strings", and these appear in 187
scenario expectations, the API contract and the stored rows of every incident
ever raised. `DIAGNOSING` is added *beside* `ROOT_CAUSE_IDENTIFIED` because they
are genuinely different moments — the activity and its result — and keeping both
is what lets an incident that diagnosed and concluded nothing be told from one
that never got that far.

## `advance` and `transition` fail in opposite directions

`app/incidents/lifecycle.py` now exports both, and the asymmetry is the design.

`transition` **refuses loudly**. It guards the machine; a caller asking for an
illegal move has a control-plane defect and should hear about it.

`advance` **shrugs**. It is called from paths where the work has already
happened — a plan computed, a provider contacted, money moved. By the time
`EXECUTING` is recorded a provider has been called; by the time `VERIFYING` is
recorded money may have moved. An incident that could not be advanced (somebody
closed it, a step was skipped) must not raise back through a path that has
already spent money. The action record and the audit trail are the durable truth
(ADR-0029); the incident's status is a description of it.

This is the only place in the codebase where tolerating a refused control is
correct, and it is correct because the control is descriptive rather than
authorising.

## The phase hook is a callback

`AgentRuntime` reports reaching a phase; it does not move incidents. The runtime
serves incident investigations *and* merchant questions that have no incident,
and it has no business knowing which. The caller that owns an incident is the
caller that may move one — and the hook is non-fatal for the same reason
`_mirror_to_stream` is: an investigation is the work, and a status is a
description of it.

## What the mutation run taught

Nine mutants, 7/9 first time. Both survivors were instructive, and in different
ways.

### An untested branch

`let an unknown external state skip reconciliation` survived because **every
test took the SUCCESS path**. `RECONCILING` is only reached when verification
returns `UNKNOWN`, which needs a `TIMEOUT_AFTER_SUBMIT` fault — §53's case,
where the refund may or may not have happened. The whole UNKNOWN branch of the
execution tail was unverified. The test added asserts both halves: the incident
reaches `RECONCILING`, and it does **not** reach `MEASURING`, because an
undetermined outcome must not claim to have been measured.

### A mutant that found redundant code, then took three attempts to express

`let advance move an incident illegally` flipped `if to in legal_from(...)` to
`if True:` and nothing changed — `transition` already checks legality before it
mutates anything, so the pre-check duplicated a check three lines away.
Removing a check that duplicates another check is unobservable by construction.
The redundancy was the finding; the pre-check is gone, and `advance` now has one
authority on legality rather than two.

Replacing it took two more goes, both worth recording because both failed the
same way:

1. Narrowing `except IllegalTransition` to a class never raised — unobservable,
   because the broad `except Exception` below it caught what the narrowed clause
   missed.
2. Re-raising from *inside* the clause — observable, because a sibling `except`
   does not catch it. Verified by hand before trusting a run: two tests fail.

The lesson generalises past this file. **A mutant is only worth having if it
changes something a test could see**, and "I broke a check" is not the same
claim as "I changed a behaviour" when the check is redundant or a broader
handler stands behind it.

### The coverage shape

All nine were caught by **unit tests and zero scenarios**. The incident
lifecycle is graded by pytest rather than by the scenario suite, which is
reasonable — the scenario suite grades what the *agent* does, and these are
control-plane moves the agent never makes — but it is worth knowing that a
scenario-only run would prove nothing about §20.

## Consequences

- Every state in the enum is now reachable, and
  `test_every_state_in_the_enum_is_reachable_or_a_start` keeps it that way. That
  test would have failed before this change.
- The phases are recorded *when they happen*, not reconstructed at the end. A
  reader of §65's timeline sees a run progressing rather than a block of states
  written at one instant.
- Both new investigation phases are **skippable**. A run that answers from state
  it already had makes no tool calls; one that produces no output block never
  diagnoses. Requiring the full chain would strand exactly the runs that did
  least.
- `PARTIAL` verification moves the incident nowhere. It is neither settled nor
  unresolvable, and sending it to `MEASURING` or `RECONCILING` would assert
  something nobody established.
- The enum is stored as a string (`native_enum=False`), so the added members
  need no migration. Existing rows keep their values and remain legal.
