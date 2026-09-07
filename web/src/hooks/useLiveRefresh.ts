// The one live-refresh hook — plan P0-06.
//
// Three screens each grew their own polling loop, and each got a different
// subset of the rules right. `Operations` paused on hidden tabs; `TaskDetail`
// stopped when a task could no longer change but had no notion of a stale
// read; nothing anywhere prevented a slow request from overlapping the next
// tick, showed when the data was last good, or distinguished "paused" from
// "the API is gone".
//
// The plan states the rules as a list, and a list of rules implemented three
// times is a list of rules implemented once and imitated twice. So they live
// here:
//
//   pause hidden tabs                document.visibilityState
//   refresh immediately on return    a visible tab shows current data
//   prevent overlapping requests     one in flight at a time, always
//   show last-updated time           `updatedAt`, from a successful read only
//   show live/paused state           `live`
//   show connectivity state          `error`, and the previous data is KEPT
//   never fake activity              nothing here ticks a clock or animates
//
// The one that is easy to get wrong is the sixth. A failed background poll must
// not blank the screen: an operator reading a queue when the API hiccups should
// keep the queue and be told it is stale, not lose it and be told nothing. So a
// failure sets `error` and leaves `data` exactly as it was.

import { useCallback, useEffect, useRef, useState } from "react";

export interface LiveRefresh<T> {
  data: T | null;
  /** The last error, if the most recent attempt failed. Cleared by a success.
   *  `data` survives it — see above. */
  error: unknown;
  /** True until the first attempt settles. Distinct from "refreshing": a
   *  screen that has never loaded shows a skeleton, one that is refreshing
   *  shows its data. */
  loading: boolean;
  /** A refresh is in flight over data that is already on screen. */
  refreshing: boolean;
  /** When the data currently on screen was fetched. Null until a read
   *  succeeds — an attempt that failed did not update anything. */
  updatedAt: Date | null;
  /** Whether the timer is running. False on a hidden tab, or when the caller
   *  passed `enabled: false` because the thing being watched can no longer
   *  change. */
  live: boolean;
  /** Fetch now, out of band. Safe to call from a button; it will not stack up
   *  behind an in-flight request. Clears any backoff. */
  refresh: () => Promise<void>;
  /** How many attempts have failed in a row. Zero once one succeeds. */
  failures: number;
  /** The interval currently in use, which is longer than `intervalMs` while
   *  the API is failing. Shown, so a slowed-down screen does not look frozen. */
  currentIntervalMs: number;
}

export interface LiveRefreshOptions {
  /** Milliseconds between polls. The plan's recommended cadences: 5–10s for the
   *  Command Center, 5s for incidents, 2–5s for actions. */
  intervalMs: number;
  /** Stop polling without unmounting — a settled task cannot change, and
   *  polling it forever is load with no possible new information. The data
   *  already fetched stays on screen. */
  enabled?: boolean;
  /** Values the fetcher depends on. Changing any of them fetches immediately.
   *
   *  This is not optional bookkeeping. The fetcher is held in a ref so that a
   *  parent re-render does not restart the interval — and the consequence is
   *  that a *genuine* change of input would otherwise be invisible: switching
   *  the queue from "escalated" to "all unsettled" would leave the old list on
   *  screen until the next tick happened to fire, showing one filter's rows
   *  under the other filter's heading.
   *
   *  So inputs are declared. An empty array is the honest default for a
   *  fetcher that takes nothing. */
  deps?: readonly unknown[];
}

// How far the interval may stretch while the API is failing, and how fast.
// Doubling from the screen's own cadence, capped at a minute.
//
// A queue polling every four seconds becomes, against a dead API, fifteen
// requests a minute per open tab — from every operator who had it open when it
// went down. That is load arriving exactly when the thing cannot take it, and
// none of it can succeed. Backing off is the difference between a browser
// waiting and a browser contributing to the outage.
//
// Capped at a minute rather than growing without bound: an API that comes back
// should be noticed within a minute, and a screen that has quietly stretched
// to a ten-minute poll is a screen showing stale data with a live-looking
// indicator.
const MAX_BACKOFF_MS = 60_000;

/** `document.hidden`, guarded — some embedded webviews have no `document`. */
function hidden(): boolean {
  return typeof document !== "undefined" && document.hidden;
}

function backoff(base: number, failures: number): number {
  if (failures === 0) return base;
  return Math.min(base * 2 ** failures, MAX_BACKOFF_MS);
}

export function useLiveRefresh<T>(
  fetcher: () => Promise<T>,
  { intervalMs, enabled = true, deps = [] }: LiveRefreshOptions,
): LiveRefresh<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [live, setLive] = useState(false);
  const [failures, setFailures] = useState(0);

  // The fetcher usually closes over props and is therefore a new function on
  // every render. Held in a ref so the polling effect does not restart — and
  // reset the interval — on every parent re-render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // "One request in flight" has to be a ref rather than state: two ticks in the
  // same frame both read stale state and both proceed, which is the overlap
  // this is meant to prevent.
  const inFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const next = await fetcherRef.current();
      if (!mounted.current) return;
      setData(next);
      setUpdatedAt(new Date());
      setError(null);
      // One success ends the backoff outright rather than stepping down. A
      // recovered API should be polled at the screen's real cadence
      // immediately; easing back would leave the busiest screen the slowest.
      setFailures(0);
    } catch (e) {
      // Deliberately does not touch `data`. Losing a queue because one poll
      // failed is worse than showing it with a staleness marker.
      if (mounted.current) {
        setError(e);
        setFailures((n) => n + 1);
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) {
        setRefreshing(false);
        setLoading(false);
      }
    }
  }, []);

  const period = backoff(intervalMs, failures);

  // Two effects, not one, and the split is the whole reason the backoff works.
  //
  // The interval has to re-arm when `period` widens — that IS the mechanism.
  // But an effect that both re-arms the timer AND fetches on entry would fetch
  // every time the period changed, and the period changes on every failure. A
  // failing API would then be polled MORE than a healthy one: exactly backwards,
  // and exactly what the first version of this did.
  //
  // So: one effect owns "fetch now" (mount, a changed input, a tab coming
  // back), and one owns the timer.

  // --- fetch now, and watch for the tab coming back ---------------------
  useEffect(() => {
    if (!enabled) {
      setLive(false);
      // Still load once. A screen that is disabled from the start — a task that
      // was already settled when it was opened — must show its data.
      void refresh();
      return;
    }

    const onVisibility = () => {
      const visible = !document.hidden;
      setLive(visible);
      // Immediately on return, before the first interval elapses. Coming back
      // to a tab and waiting five seconds to find out the queue changed is the
      // same as not polling.
      if (visible) void refresh();
    };

    setLive(!document.hidden);
    void refresh();

    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
    // `updatedAt` and `period` are deliberately absent: this effect must run on
    // mount and on a changed input, and on nothing else.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, refresh, ...deps]);

  // --- the timer ---------------------------------------------------------
  useEffect(() => {
    if (!enabled || hidden()) return;
    const timer = setInterval(() => {
      // Re-checked at fire time as well as at arm time: a tab hidden between
      // the two would otherwise keep polling until the next re-arm.
      if (!document.hidden) void refresh();
    }, period);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, period, refresh, live, ...deps]);

  return {
    data, error, loading, refreshing, updatedAt, live, refresh,
    failures, currentIntervalMs: period,
  };
}

/** How long ago, in words, for the "last updated" line.
 *
 *  Seconds up to a minute, then minutes, then the clock time. An operator
 *  glancing at a queue wants to know whether it is current, and "14:03:11" does
 *  not answer that without arithmetic — while "4 minutes ago" stops being
 *  useful once it is hours. */
export function ago(when: Date | null, now: Date = new Date()): string {
  if (!when) return "never";
  const seconds = Math.max(0, Math.round((now.getTime() - when.getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return when.toLocaleTimeString();
}
