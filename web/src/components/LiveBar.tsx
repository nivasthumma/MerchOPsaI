// The freshness line — plan P0-06 and P1-13.
//
// Three facts an operator needs about any auto-refreshing screen, and which
// nothing in this app used to state:
//
//   when the data on screen was last good
//   whether it is still being refreshed
//   whether the last attempt failed
//
// The third is the one that matters. A queue that stopped updating because the
// API is unreachable looks exactly like a queue where nothing is happening —
// and "nothing is happening" is the most reassuring thing a financial
// operations console can say, so it must never be said by accident.
//
// P1-13's example is followed literally: an unreachable API shows what failed,
// when the data was last good, and a retry. It never shows a bare "failed to
// fetch", and it never implies an unsafe retry happened on its own.

import { ago, type LiveRefresh } from "../hooks/useLiveRefresh";

export function LiveBar<T>({ live, what = "data" }:
                           { live: LiveRefresh<T>; what?: string }) {
  const { error, updatedAt, refreshing, live: polling, refresh, data,
          failures, currentIntervalMs } = live;

  return (
    <div className="livebar" role="status" aria-live="polite">
      <span className={`livedot ${error ? "is-error" : polling ? "is-live" : "is-paused"}`}
            aria-hidden="true" />

      {error ? (
        <>
          <strong>Cannot reach the API.</strong>
          <span className="muted">
            {updatedAt
              ? <>Showing {what} from {updatedAt.toLocaleTimeString()} ({ago(updatedAt)}).</>
              : <>Nothing has loaded yet.</>}
          </span>
          {/* Said explicitly, because the alternative is an operator assuming
              it. A read that failed changed nothing; the plan's rule is that a
              provider failure must state that no unsafe retry occurred. */}
          <span className="muted">No action was taken.</span>
          {failures > 1 ? (
            // A slowed screen must not read as a frozen one. Saying the
            // interval out loud is also the honest version of "live": this is
            // still polling, just not as often, and an operator who wants it
            // now has the button.
            <span className="muted">
              Retrying every {Math.round(currentIntervalMs / 1000)}s after{" "}
              {failures} failed attempts.
            </span>
          ) : null}
        </>
      ) : (
        <>
          <span className="muted">
            Updated {ago(updatedAt)}
            {updatedAt ? <> · <span className="mono">{updatedAt.toLocaleTimeString()}</span></> : null}
          </span>
          <span className="muted">
            {/* "Paused" is the truth on a hidden tab, and saying so is what
                stops a returning operator from trusting a stale screen. The
                hook refreshes on return, so this state is short-lived and
                honest while it lasts. */}
            {polling ? (refreshing ? "refreshing…" : "live") : "paused"}
          </span>
        </>
      )}

      <button className="linkish" onClick={() => void refresh()}
              disabled={refreshing}
              aria-label={`Refresh ${what} now`}>
        {refreshing ? "Refreshing…" : "Refresh"}
      </button>

      {/* Only when there is nothing on screen at all. With data present the
          message above already says it is stale, and a second banner saying the
          same thing louder is the wall of warnings this design avoids. */}
      {error && !data ? (
        <span className="muted">
          {(error as { message?: string }).message ?? "Unknown error."}
        </span>
      ) : null}
    </div>
  );
}
