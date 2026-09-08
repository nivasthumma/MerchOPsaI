// The live-refresh rules — plan P0-06.
//
// Three screens depend on this hook, so its rules are tested directly rather
// than only through whichever screen happens to exercise them. Each test is
// named after one line of the plan's list.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ago, useLiveRefresh } from "./useLiveRefresh";

/** Drive `document.hidden` the way a browser does. jsdom exposes it as a
 *  getter on the prototype, so it is redefined rather than assigned. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true, get: () => hidden,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setHidden(false);
});
afterEach(() => {
  vi.useRealTimers();
  setHidden(false);
});

describe("polling", () => {
  it("fetches once on mount and then on the interval", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => useLiveRefresh(fetcher, { intervalMs: 1000 }));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("never overlaps two requests", async () => {
    // The rule that is easiest to get wrong and hardest to see: a slow request
    // and a fast interval stack up, and the responses arrive out of order.
    let release: (v: number) => void = () => {};
    const fetcher = vi.fn(() => new Promise<number>((r) => { release = r; }));
    renderHook(() => useLiveRefresh(fetcher, { intervalMs: 50 }));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => { release(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("pauses on a hidden tab and refreshes the moment it returns", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 1000 }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    act(() => setHidden(true));
    await waitFor(() => expect(result.current.live).toBe(false));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Immediately, not on the next tick. Coming back to a tab and waiting for
    // the interval is the same as not polling.
    act(() => setHidden(false));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    expect(result.current.live).toBe(true);
  });

  it("does not poll when it is disabled, but still loads once", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100, enabled: false }));

    // A task that was already settled when it was opened must still show its
    // data; it simply has nothing new to say afterwards.
    await waitFor(() => expect(result.current.data).toBe(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.live).toBe(false);
  });

  it("refetches at once when a declared input changes", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    let scope = "escalated";
    const { rerender } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 10_000, deps: [scope] }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    // Without this, switching a filter would leave one filter's rows under the
    // other filter's heading until an interval happened to fire.
    scope = "all";
    rerender();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });
});

describe("failure", () => {
  it("keeps the data on screen and reports the error", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce("rows")
      .mockRejectedValue(new Error("network"));
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));
    await waitFor(() => expect(result.current.data).toBe("rows"));

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await waitFor(() => expect(result.current.error).toBeTruthy());
    // Losing a queue because one poll failed is worse than showing it stale.
    expect(result.current.data).toBe("rows");
  });

  it("does not advance the last-updated time on a failed attempt", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce("rows")
      .mockRejectedValue(new Error("network"));
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));
    await waitFor(() => expect(result.current.data).toBe("rows"));
    const first = result.current.updatedAt;

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await waitFor(() => expect(result.current.error).toBeTruthy());
    // An attempt that failed did not update anything, and a freshness stamp
    // that moves on failure is a screen claiming to be current when it is not.
    expect(result.current.updatedAt).toBe(first);
  });

  it("clears the error once a later attempt succeeds", async () => {
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue("rows");
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));
    await waitFor(() => expect(result.current.error).toBeTruthy());

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await waitFor(() => expect(result.current.data).toBe("rows"));
    expect(result.current.error).toBeNull();
  });
});

describe("backing off a failing API", () => {
  it("does not poll a dead API at full speed", async () => {
    // A queue on a four-second cadence is fifteen requests a minute per open
    // tab against an API that cannot answer any of them — load arriving
    // exactly when the thing cannot take it.
    const fetcher = vi.fn().mockRejectedValue(new Error("down"));
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));

    await waitFor(() => expect(result.current.failures).toBe(1));
    expect(result.current.currentIntervalMs).toBe(200);

    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    await waitFor(() => expect(result.current.failures).toBe(2));
    expect(result.current.currentIntervalMs).toBe(400);
  });

  it("widening the interval does not itself trigger a fetch", async () => {
    // The bug this pins, which the first version had: the timer effect
    // re-arms when the period changes, and the period changes on every
    // failure. An effect that also fetched on entry would poll a FAILING api
    // more often than a healthy one — exactly backwards.
    const fetcher = vi.fn().mockRejectedValue(new Error("down"));
    renderHook(() => useLiveRefresh(fetcher, { intervalMs: 1000 }));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    // Nothing advances the clock, so nothing may call again however many times
    // the period recalculates.
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("is capped, so a recovered API is noticed within a minute", async () => {
    // Unbounded doubling turns a screen into a ten-minute poll showing stale
    // data behind a live-looking indicator.
    const fetcher = vi.fn().mockRejectedValue(new Error("down"));
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 10_000 }));

    for (let i = 0; i < 6; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(result.current.currentIntervalMs);
      });
    }
    expect(result.current.currentIntervalMs).toBeLessThanOrEqual(60_000);
  });

  it("one success ends the backoff outright", async () => {
    // Not a step down: a recovered API should be polled at the screen's real
    // cadence immediately, or the busiest screen stays the slowest.
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("down"))
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue("rows");
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));

    await waitFor(() => expect(result.current.failures).toBe(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    await waitFor(() => expect(result.current.failures).toBe(2));

    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    await waitFor(() => expect(result.current.data).toBe("rows"));
    expect(result.current.failures).toBe(0);
    expect(result.current.currentIntervalMs).toBe(100);
  });

  it("an explicit refresh is not delayed by the backoff", async () => {
    // The operator pressing the button has decided the wait is over.
    const fetcher = vi.fn().mockRejectedValue(new Error("down"));
    const { result } = renderHook(
      () => useLiveRefresh(fetcher, { intervalMs: 100 }));
    await waitFor(() => expect(result.current.failures).toBe(1));

    await act(async () => { await result.current.refresh(); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});

describe("ago", () => {
  it("reads as recency, not as a clock", () => {
    const now = new Date("2026-09-07T12:00:00Z");
    expect(ago(null)).toBe("never");
    expect(ago(new Date("2026-09-07T11:59:58Z"), now)).toBe("just now");
    expect(ago(new Date("2026-09-07T11:59:30Z"), now)).toBe("30s ago");
    expect(ago(new Date("2026-09-07T11:56:00Z"), now)).toBe("4m ago");
    // Past an hour "ago" stops helping and the clock time starts.
    expect(ago(new Date("2026-09-07T09:00:00Z"), now)).toMatch(/:/);
  });

  it("never reports a negative age from a clock that ran backwards", () => {
    const now = new Date("2026-09-07T12:00:00Z");
    expect(ago(new Date("2026-09-07T12:00:30Z"), now)).toBe("just now");
  });
});
