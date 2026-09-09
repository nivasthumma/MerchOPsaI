// The seeded tenant has no agent actions, so the captured `/provider-health`
// response is the EMPTY one — that is the fixture. The populated cases are
// constructed, and marked.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderHealth } from "../api/types";
import Provider from "./Provider";
import emptyFixture from "../test-fixtures/provider-health.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { providerHealth: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const EMPTY = emptyFixture as unknown as ProviderHealth;

/** Constructed: four refunds with one of each outcome, so the columns cannot
 *  be confused for one another. */
const BUSY: ProviderHealth = {
  ...EMPTY,
  operations: [
    { action_type: "refund", attempted: 4, succeeded: 2, failed: 1, unknown: 1,
      p50_latency_ms: 120, p95_latency_ms: 8000 },
    { action_type: "payment_link", attempted: 1, succeeded: 1, failed: 0,
      unknown: 0, p50_latency_ms: 60, p95_latency_ms: 60 },
  ],
  history: [
    { day: "2026-09-07", attempted: 1, succeeded: 1, failed: 0, unknown: 0 },
    { day: "2026-09-08", attempted: 2, succeeded: 0, failed: 1, unknown: 1 },
    { day: "2026-09-09", attempted: 2, succeeded: 2, failed: 0, unknown: 0 },
  ],
  webhooks_received: 12,
  webhooks_rejected: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.providerHealth.mockResolvedValue(BUSY);
});

const show = async () => {
  render(<Provider />);
  await screen.findByRole("heading", { name: /By operation/ });
};

describe("provider health", () => {
  it("breaks outcomes down per operation", async () => {
    await show();
    // A provider can be fine on one call and not another, and an overall rate
    // hides exactly that.
    expect(screen.getByText("refund")).toBeInTheDocument();
    expect(screen.getByText("payment_link")).toBeInTheDocument();
  });

  it("gives UNKNOWN its own column and never folds it into failure", async () => {
    await show();
    const row = screen.getByText("refund").closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);

    // attempted, succeeded, failed, unknown — four separate figures. A success
    // rate of succeeded/attempted would quietly call the unknown a failure.
    expect(cells.slice(1, 5)).toEqual(["4", "2", "1", "1"]);
  });

  it("does not compute a success rate at all", async () => {
    await show();
    // Any single percentage has to decide what UNKNOWN counts as, and this page
    // refuses to make that decision on the reader's behalf.
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("shows provider latency, not our verification time", async () => {
    await show();
    const row = screen.getByText("refund").closest("tr") as HTMLElement;
    expect(within(row).getByText("120 ms")).toBeInTheDocument();
    // p95 carries the slow UNKNOWN, which is the point of showing a tail.
    expect(within(row).getByText("8000 ms")).toBeInTheDocument();
  });

  it("gives a history rather than only a verdict", async () => {
    await show();
    // "Is it reachable now" is the load balancer's question. An operator asks
    // whether now is unusual.
    expect(screen.getByLabelText("Daily provider outcomes")).toBeInTheDocument();
    expect(screen.getByText("2026-09-08")).toBeInTheDocument();
  });

  it("says plainly that execution is mocked", async () => {
    await show();
    // Not implied by a green light. A mock adapter is not a provider that
    // answered.
    expect(screen.getByText("execution is mocked")).toBeInTheDocument();
    expect(screen.getByText(/only the\s+outbound call differs/)).toBeInTheDocument();
  });

  it("counts inbound webhooks separately from outbound calls", async () => {
    await show();
    // A provider can be healthy outbound and silent inbound, and that asymmetry
    // is invisible from actions alone.
    expect(screen.getByText("Webhooks in")).toBeInTheDocument();
    expect(screen.getByText("Rejected")).toBeInTheDocument();
  });

  it("changes the window", async () => {
    await show();
    await userEvent.click(screen.getByRole("button", { name: "30 days" }));
    expect(mocked.providerHealth).toHaveBeenLastCalledWith(30);
  });

  it("says so when nothing happened in the window", async () => {
    // The captured response for the seeded tenant.
    mocked.providerHealth.mockResolvedValue(EMPTY);
    render(<Provider />);

    expect(await screen.findByText(/No provider calls in this window/))
      .toBeInTheDocument();
  });

  it("surfaces a failure rather than an empty page", async () => {
    mocked.providerHealth.mockRejectedValue(new ApiError(500, "boom"));
    render(<Provider />);
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});
