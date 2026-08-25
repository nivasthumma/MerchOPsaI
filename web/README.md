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
  api/types.ts      response shapes, mirroring app/api/main.py
  App.tsx           shell, run-configuration banners, token gate
  routes/
    Investigate     ask a question, read findings and grounding
    TaskDetail      approval gate, actions, verification, replay, audit trace
    Scenarios       browse and run the 106 evaluation scenarios
    Operations      reconciliation sweep and the escalated operator queue
  components/Bits   status pills, money formatting, error banners
```

## Test

```bash
npm test             # 39 Vitest tests, jsdom, no API required
npm run test:watch
```

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

They are not in CI (see ADR-0015), so they gate a developer's machine, not a merge.

## Build

```bash
npm run build        # tsc, then vite build -> dist/
npm run typecheck
```

Node 18 is what this was built against, so Vite is pinned to 5.x (7.x requires Node 20+).
