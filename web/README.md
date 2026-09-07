# MerchantOps Agent — React SPA

A single-page control plane for the agent: ask a question, read the evidence, approve or
reject what the policy engine stopped, watch verification resolve, and replay a task
without moving money.

> **Scope note.** CONTRACT §3 lists a React UI under *designed, not built*, and §52
> excludes it from the MVP. This exists because it was explicitly requested; the
> deviation is recorded in [ADR-0015](../docs/adr/0015-react-spa-frontend.md). The
> Streamlit UI remains and is still the contract-conformant one.

## Run it

```bash
make api                 # FastAPI on :8000        (from the repository root)
make web                 # Vite dev server on :5173
make token USER_ID=USR_A_OWNER   # mint a bearer token, paste it into the app
```

Then open http://127.0.0.1:5173.

## Why there is no CORS middleware

The dev server proxies `/api` to `127.0.0.1:8000`, so the browser only ever makes
same-origin requests. Adding permissive CORS to an API whose entire premise is that
authorization lives server-side would widen its attack surface to save a proxy rule.
For deployment, serve `dist/` behind the same origin as the API.

## What the UI will not do

The design constraint is the same one the backend has: **the frontend is never the
authority.**

- It does not decide whether an action is permitted. It renders what policy returned.
- It does not hide the approve button when it thinks you lack permission — the server
  re-checks authorization on approval and returns 409 with a reason, which is displayed.
- It does not translate `UNKNOWN` into something more comfortable. An unsettled action
  is shown as unsettled, with the re-verify path next to it.
- It stores the bearer token in `localStorage` and sends it to this API only. The token
  carries identity, never permissions.

## Layout

```
src/
  api/client.ts     typed fetch wrapper: auth header, error normalisation
  api/types.ts      response shapes, mirroring app/api/schemas.py
  App.tsx           shell, run-configuration banners, token gate, nav IA
  hooks/
    useLiveRefresh  the ONE polling loop (see below)
  routes/
    CommandCenter   home: what needs attention, revenue health, the funnel
    Actions         the Action Center — five sections, UNKNOWN as work
    Recovery        the revenue ledger: at risk → recoverable → attempted → recovered
    PaymentLifecycle  §7 — one payment end to end; where a searched id lands
    Incidents       open incidents, filtered and saved-view
    IncidentDetail  the decision workspace (what happened → ... → verification)
    Investigate     ask a question, read findings and grounding
    TaskDetail      approval gate, actions, verification, replay, audit trace
    Scenarios       browse and run the evaluation scenarios
    Operations      reconciliation sweep and the escalated operator queue
  components/
    Bits            money formatting, error banners, copyable ids
    Status          the ONE status vocabulary (see below)
    LiveBar         when the data was last good, and whether it still is
```

## Responsive and accessible, where it is checkable

jsdom does no layout, so nothing here can assert what a 375px viewport *looks*
like. What is asserted is the contract the CSS depends on, which is the half a
change breaks silently.

**P1-11.** Below 760px a `table.stacked` stops being a grid and becomes one
block per row, each cell labelled by the header it belongs to. The Action Center
is twelve columns wide; on a phone the previous treatment was a horizontal
scrollbar with a table hidden behind it, and reading a refund's verification
state meant swiping past six columns. A cell may be marked
`data-priority="low"` and dropped entirely at that width — and a test asserts
that nothing which asserts something about money ever is.

**P1-12.** `role="dialog" aria-modal="true"` is a promise that everything
outside the dialog is inert, and **both** dialogs in this app declared it while
keeping different halves of it. The action drawer kept none: focus stayed on
the row behind, Escape did nothing, Tab walked off into the supposedly-inert
page, and a keyboard user could open it and not get out. The command palette
focused its input and stopped there.

`hooks/useModalFocus` is the one implementation, for the same reason
`useLiveRefresh` is: a rule written twice is a rule that holds in one place.
Focus moves in, Tab wraps at both ends, Escape closes, and focus returns to
whatever opened it — `<body>` otherwise, which makes the next Tab restart from
the top of the page. Where focus lands is the caller's choice and worth
choosing: the drawer takes the panel, because its first control is Close and
landing there announces "close" before saying what was opened; the palette
takes its input, because typing is the entire reason to open it.

## What a failure says about the world — P1-13

The plan asks that a provider failure "explicitly state that no unsafe retry
occurred". An operator's real question is narrower and harder: *did this happen
or not*, and their default assumption after seeing an error is that it did not.
For a write that failed after being sent, that assumption is exactly wrong.

So `ApiError` carries the request method and derives an `effect`:

    refused     a 4xx — the server decided, nothing ran
    read-only   a read; whatever happened to it, it changed nothing
    unknown     a write that failed at the transport or with a 5xx. It may
                have been applied.

The banner states the consequence under the error rather than leaving it to the
reader, and never behind a disclosure: the operator who does not expand it is
the one most likely to press the button again. The `unknown` copy points at the
Action Center, because this system already has a name and a queue for that
state.

Refusing to send for want of a token is `refused`, not `read-only` — nothing
was sent at all, which is a stronger claim and the right one to make.

## No optimistic financial success — P1-14

Re-verify is the button people press *because* an outcome is unresolved, and it
was reporting `Re-verify done.` in a green tone on an HTTP 200. Re-verification
can come back UNKNOWN. Green there tells an operator the question was answered
when all that happened is that it was asked.

It now renders what the read found, and the tone is the load-bearing part —
a green toast is a claim that the matter is settled:

    SUCCESS   ok     the money moved
    FAILED    warn   settled, and correct: it did not take effect, nothing is
                     outstanding. Not red — red would file it beside the states
                     that need somebody.
    PARTIAL   warn   the provider reflects less than was requested
    UNKNOWN   warn   still unestablished — and it says nothing was re-issued,
                     because "unknown" on its own invites a second press

## Two things that are deliberately singular

**`hooks/useLiveRefresh`.** Three screens each grew their own polling loop and each got
a different subset of the rules right — pause on a hidden tab, refresh on return, never
overlap two requests, show when the data was last good, distinguish paused from
disconnected, never fake activity. A list of rules implemented three times is a list
implemented once and imitated twice, so there is one hook and it has its own tests.

The rule that matters most: **a failed poll does not blank the screen.** An operator
reading a queue when the API hiccups keeps the queue and is told it is stale. Losing it
would be worse, because an empty queue is the most reassuring thing this application can
say and it must never be said by accident.

It also **backs off while the API is failing** — doubling from the screen's own
cadence, capped at a minute. The Action Center polls every four seconds; against
a dead API that is fifteen requests a minute per open tab, from every operator
who had it open when it went down, none of which can succeed. The cap exists so
a recovered API is noticed within a minute rather than by a screen that has
quietly stretched to a ten-minute poll behind a live-looking indicator. One
success ends the backoff outright rather than stepping down, or the busiest
screen would stay the slowest. The freshness bar says the interval out loud,
because a slowed screen must not read as a frozen one.

**`components/Status`.** Thirteen statuses appear across these screens, and every one
now has a tone, a business-language label, a *shape*, and a sentence saying what it
asserts. The shape is not decoration: a red dot meaning "it failed" and a red dot
meaning "we do not know" are the same dot, and those are opposite claims about whether
money moved.

## Test

```bash
npm test             # 293 Vitest tests, jsdom, no API required
npm run test:watch
```

> **One fixture is not a live response, despite the header above saying they are.**
> `task.json` carries `intent: duplicate_payment` *and* a completed, approved,
> verified refund. No single request produces both: the deterministic planner sets
> `intent`/`recommendation`/`agent_confidence` only on the revenue-investigation path,
> and that path proposes no refund. It is a composite, assembled or captured under
> older behaviour, and three test files read it. Splitting it into two genuinely
> captured fixtures — one completed-with-conclusion, one completed-refund — is real
> work and is not done. Found 2026-09-07 while adding P0-08; left as it was rather
> than reshaped, because reshaping it silently changes what those three files cover.

The tests cover the places where a frontend bug would misrepresent a financial state
rather than merely look wrong:

| Area | What is pinned |
|---|---|
| API client | The bearer token is sent; a request without one never leaves the browser; a 409 from the approval state machine keeps its `code`; a dead API says so instead of "Failed to fetch"; task ids are percent-encoded |
| Verification | `UNKNOWN` renders as `UNKNOWN` and never carries a success tone; `PARTIAL` is distinct from `SUCCESS`; a settled action offers no re-verify button |
| Money | Minor units convert exactly, keep sub-rupee precision, and a missing amount is not rendered as zero |
| Approval | The approve button stays enabled — authorization is the server's call; a refusal shows its code; the task is reloaded rather than trusting the response |
| Replay | Zero external calls reads as correct; a non-zero count reads as a defect |
| Shell | The mock adapter, the deterministic planner, and a development signing secret are each stated before anyone can act |
| Action Center | An action is in exactly one section — including the case that broke it, an escalated action a later re-verification settled; the attempt limit is read from the response rather than copied; a pending approval is not rendered as an action |
| Live refresh | A failed poll keeps the data and does not advance the freshness stamp; a hidden tab pauses and refreshes on return; two requests never overlap; a changed filter refetches at once; a failing API is backed off, the backoff is capped, widening it never itself triggers a fetch, and one success clears it |
| Funnel | A later stage never draws wider than an earlier one, even when handed figures that invert |
| Incident | The page is ordered as the decision is made; a single evidence source is stated to corroborate nothing; a rule that publishes no baseline says so rather than showing a zero |
| Lifecycle | Events render in the server's order and are never re-sorted into the sequence they "usually" occur in; a policy-gated tool call is not shown as failed; an unmapped payment says it cannot be executed against rather than showing a dash |
| Small screens (P1-11) | Every cell of a stacked table carries the header it belongs to, because the header row is not rendered at that width; no cell asserting something about money is ever marked droppable; every label matches a real column |
| Verification outcome (P1-14) | Re-verify reports what the read FOUND, never that it ran: a still-UNKNOWN result is not given a success tone, a verified FAILED is settled rather than an alarm, and PARTIAL is distinguished from both |
| Failure consequence (P1-13) | A failed request says whether anything happened: a read changed nothing, a 4xx was refused before doing anything, and a write that failed *after* being sent is honestly `unknown` and points at the UNKNOWN queue rather than inviting a second press |
| Dialogs (P1-12) | `aria-modal` is kept rather than claimed, by both dialogs from one hook: focus moves in, Escape closes, Tab wraps at both ends — including in a dialog with nothing focusable in it — and focus returns to whatever opened it |

They are not in CI (see ADR-0015), so they gate a developer's machine, not a merge.

## Build

```bash
npm run build        # tsc, then vite build -> dist/
npm run typecheck
```

Node 18 is what this was built against, so Vite is pinned to 5.x (7.x requires Node 20+).
