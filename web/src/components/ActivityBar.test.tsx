// The bar reflects the client's real in-flight count. Driving it through a fake
// subscription is the point: if the component ever starts animating on a timer
// instead of on actual work, this test stops passing.

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let emit: ((n: number) => void) | null = null;

vi.mock("../api/client", () => ({
  activity: {
    get pending() { return 0; },
    subscribe(fn: (n: number) => void) {
      emit = fn;
      fn(0);
      return () => { emit = null; };
    },
  },
}));

const { ActivityBar } = await import("./Chrome");

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

describe("activity bar", () => {
  it("stays out of the way for a fast request", async () => {
    render(<ActivityBar />);
    act(() => emit?.(1));
    // Below the delay threshold. A bar that flashes on every 40ms call is noise,
    // and noise is what teaches people to stop looking at indicators.
    await act(async () => { vi.advanceTimersByTime(120); });
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("appears while work is outstanding and leaves when it finishes", async () => {
    render(<ActivityBar />);
    act(() => emit?.(1));
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(screen.getByRole("progressbar")).toBeInTheDocument();

    act(() => emit?.(0));
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
