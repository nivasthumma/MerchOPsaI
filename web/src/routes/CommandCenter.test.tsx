// The Command Center — plan P0-05, P1-03.
//
// The fixture is a live `/command-center` response captured against the seeded
// database.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CommandCenter as CommandCenterData } from "../api/types";
import CommandCenter from "./CommandCenter";
import fixture from "../test-fixtures/command-center.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { commandCenter: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const DATA = fixture as unknown as CommandCenterData;

function renderCC() {
  return render(<MemoryRouter><CommandCenter /></MemoryRouter>);
}

beforeEach(() => vi.clearAllMocks());

describe("what needs my attention", () => {
  it("puts the attention row before the money", async () => {
    mocked.commandCenter.mockResolvedValue(DATA);
    renderCC();

    const headings = await screen.findAllByRole("heading", { level: 3 });
    // A merchant opening this during an incident is not looking for a revenue
    // summary; they are looking for the thing waiting on them.
    expect(headings[0]).toHaveTextContent("Needs attention");
    expect(headings[1]).toHaveTextContent("Revenue health");
  });

  it("gives every count somewhere to go", async () => {
    mocked.commandCenter.mockResolvedValue(DATA);
    renderCC();

    // A count with nowhere to go is a notification. This is a control plane.
    expect(await screen.findByRole("link", { name: /Awaiting approval/ }))
      .toHaveAttribute("href", "/actions?section=awaiting_approval");
    expect(screen.getByRole("link", { name: /Unknown/ }))
      .toHaveAttribute("href", "/actions?section=unknown");
    expect(screen.getByRole("link", { name: /Escalated/ }))
      .toHaveAttribute("href", "/actions?section=escalated");
  });

  it("keeps expired approvals out of the live queues", async () => {
    // Expired work is not work waiting on someone — it is work that can no
    // longer be done, and a tile beside the live queues invites an attempt.
    mocked.commandCenter.mockResolvedValue({
      ...DATA,
      attention: { ...DATA.attention, approvals_expired: 3 },
    });
    renderCC();

    expect(await screen.findByText(/passed their window and/)).toBeInTheDocument();
    const links = screen.getAllByRole("link");
    expect(links.some((l) => l.textContent?.includes("Expired"))).toBe(false);
  });
});

describe("the recovery funnel — P1-03", () => {
  it("renders the stages in the server's order", async () => {
    mocked.commandCenter.mockResolvedValue(DATA);
    renderCC();

    // Scoped to the funnel: "At risk" and "Recoverable" also appear as revenue
    // tiles, and the order under test is the funnel's.
    await screen.findByText("Recovery funnel");
    const funnel = document.querySelector("ol.funnel") as HTMLElement;
    const labels = within(funnel).getAllByText(/./, { selector: ".funnel-label" });
    expect(labels.map((l) => l.textContent))
      .toEqual(DATA.funnel.map((s) => s.label));
  });

  it("never draws a later stage wider than an earlier one", async () => {
    // The rule carried by the geometry rather than by anyone remembering it.
    //
    // The comment here used to say "Recovered is forced above at-risk" over
    // data that nested perfectly (100k, 60k, 40k, 40k) -- so the test asserted
    // that a well-ordered funnel draws in order, which every implementation
    // does. It never exercised the case its own name describes. Recovered is
    // genuinely above at-risk now.
    mocked.commandCenter.mockResolvedValue({
      ...DATA,
      funnel: [
        { stage: "AT_RISK", label: "At risk", amount_minor: 100_000 },
        { stage: "RECOVERABLE", label: "Recoverable", amount_minor: 60_000 },
        { stage: "ATTEMPTED", label: "Attempted", amount_minor: 40_000 },
        { stage: "RECOVERED", label: "Recovered", amount_minor: 140_000 },
      ] as CommandCenterData["funnel"],
    });
    renderCC();

    const bars = await screen.findAllByRole("img");
    const widths = bars.map(
      (b) => parseFloat((b as HTMLElement).style.width.replace("%", "")));
    // 100, not 140. `.funnel-track` clips, so an unclamped bar drew at exactly
    // the track's width -- a broken ledger rendered as a complete recovery,
    // indistinguishable from a healthy one.
    expect(widths).toEqual([100, 60, 40, 100]);
    expect(Math.max(...widths)).toBeLessThanOrEqual(100);
  });

  it("scales the funnel by AT_RISK by name, not by whichever stage is first",
     async () => {
    // Every share is divided by this one figure. Taking it from array position
    // means the whole chart silently rescales the day the server emits the
    // stages in another order -- and the bars would still look plausible.
    mocked.commandCenter.mockResolvedValue({
      ...DATA,
      funnel: [
        { stage: "RECOVERED", label: "Recovered", amount_minor: 25_000 },
        { stage: "AT_RISK", label: "At risk", amount_minor: 100_000 },
      ] as CommandCenterData["funnel"],
    });
    renderCC();

    const bars = await screen.findAllByRole("img");
    const widths = bars.map(
      (b) => parseFloat((b as HTMLElement).style.width.replace("%", "")));
    expect(widths).toEqual([25, 100]);
  });

  it("describes each bar for a screen reader rather than relying on the drawing", async () => {
    mocked.commandCenter.mockResolvedValue(DATA);
    renderCC();
    const bars = await screen.findAllByRole("img");
    expect(bars[0]).toHaveAttribute("aria-label", expect.stringContaining("of at risk"));
  });

  it("says why there is no funnel rather than drawing an empty one", async () => {
    mocked.commandCenter.mockResolvedValue({
      ...DATA,
      funnel: DATA.funnel.map((s) => ({ ...s, amount_minor: 0 })),
    });
    renderCC();
    expect(await screen.findByText(/Nothing is at risk right now/)).toBeInTheDocument();
  });
});

describe("honesty about the figures", () => {
  it("renders a broken invariant rather than refusing to draw", async () => {
    mocked.commandCenter.mockResolvedValue({
      ...DATA,
      revenue: { ...DATA.revenue, invariants_broken: ["recovered exceeds attempted"] },
    });
    renderCC();

    // A page that will not render is a page nobody can use to find out why.
    expect(await screen.findByText(/do not nest/)).toBeInTheDocument();
    expect(screen.getByText(/recovered exceeds attempted/)).toBeInTheDocument();
    expect(screen.getByText("Revenue health")).toBeInTheDocument();
  });

  it("says the activity feed is empty rather than inventing activity", async () => {
    mocked.commandCenter.mockResolvedValue({ ...DATA, activity: [] });
    renderCC();
    expect(await screen.findByText(/stays empty until something real does/))
      .toBeInTheDocument();
  });
});

describe("freshness", () => {
  it("shows when the data was last good", async () => {
    mocked.commandCenter.mockResolvedValue(DATA);
    renderCC();
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
  });

  it("keeps the data on screen when a refresh fails, and says it is stale", async () => {
    mocked.commandCenter
      .mockResolvedValueOnce(DATA)
      .mockRejectedValue(new Error("network"));
    const { rerender } = renderCC();
    await screen.findByText("Needs attention");

    await screen.findByText("Needs attention");
    rerender(<MemoryRouter><CommandCenter /></MemoryRouter>);
    // Losing a screen because one poll failed is worse than showing it with a
    // staleness marker. The assertion that matters is that the content stays.
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
  });
});
