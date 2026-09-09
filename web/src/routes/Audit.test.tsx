// The seeded tenant writes no audit rows, so `/audit` returns an empty page.
// That makes the empty state the captured case and every populated one
// constructed — marked here rather than passed off as real.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import type { AuditPage } from "../api/types";
import Audit from "./Audit";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { audit: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

/** Constructed. `matched` is deliberately larger than `entries` — that gap is
 *  the thing this screen must not paper over. */
const PAGE: AuditPage = {
  entries: [
    { id: 90, event_type: "policy_changed", created_at: "2026-09-09T09:00:00+00:00",
      merchant_id: "MERCH_A", user_id: "USR_A_OWNER", task_id: null,
      incident_id: null, correlation_id: "COR_AAA", payload: {} },
    { id: 89, event_type: "user_created", created_at: "2026-09-09T08:00:00+00:00",
      merchant_id: "MERCH_A", user_id: "USR_A_OWNER", task_id: null,
      incident_id: "INC_1", correlation_id: null, payload: {} },
  ],
  matched: 7,
  next_cursor: 89,
  event_types: ["policy_changed", "user_created"],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.audit.mockResolvedValue(PAGE);
});

const show = async () => {
  render(<MemoryRouter><Audit /></MemoryRouter>);
  // Not an event name: those appear twice, once in the table and once as a
  // filter option, and the ambiguity fails the query rather than the assertion.
  await screen.findByRole("heading", { name: /Entries/ });
};

describe("audit", () => {
  it("reports what the FILTER matched, not what fits on the page", async () => {
    await show();
    // Deriving a total from the rows on screen is a defect this repository has
    // written three times, and in an audit context a reader would conclude
    // there were two events when there are seven.
    //
    // The strip's figure is asserted directly. Checking only "2 of 7" left a
    // mutant alive: that string is built from `rows.length` and `page.matched`
    // separately, so swapping the strip to `rows.length` changed nothing it
    // could see.
    const matching = screen.getByText("Matching").closest("div") as HTMLElement;
    expect(within(matching).getByText("7")).toBeInTheDocument();
    expect(screen.getByText("2 of 7")).toBeInTheDocument();
  });

  it("offers the event types the data actually has", async () => {
    await show();
    const select = screen.getByLabelText("What happened");
    const offered = within(select).getAllByRole("option").map((o) => o.textContent);
    // A hardcoded list goes stale in the direction that hides events.
    expect(offered).toEqual(["any event", "policy_changed", "user_created"]);
  });

  it("filters by who did it", async () => {
    await show();
    await userEvent.type(screen.getByLabelText("Who did it"), "USR_A_OWNER");

    expect(mocked.audit).toHaveBeenCalledWith(
      expect.objectContaining({ actor: "USR_A_OWNER" }));
  });

  it("walks backwards with a cursor rather than a page number", async () => {
    await show();
    mocked.audit.mockResolvedValue({
      ...PAGE, entries: [{ ...PAGE.entries[1], id: 88 }], next_cursor: 88,
    });

    await userEvent.click(screen.getByRole("button", { name: "Show older" }));

    // Keyset, not OFFSET: `audit_logs` is append-only and receives writes while
    // somebody reads it, and a shifting window reads as a missing event.
    expect(mocked.audit).toHaveBeenLastCalledWith(
      expect.objectContaining({ beforeId: 89 }));
  });

  it("says when there is nothing older rather than offering a dead button", async () => {
    mocked.audit.mockResolvedValue({ ...PAGE, matched: 2, next_cursor: null });
    await show();

    expect(screen.queryByRole("button", { name: "Show older" })).toBeNull();
    expect(screen.getByText(/everything matching this filter/)).toBeInTheDocument();
  });

  it("links an entry back to the object it concerns", async () => {
    await show();
    // The trail is a way in, not a dead end.
    expect(screen.getByRole("link", { name: "INC_1" }))
      .toHaveAttribute("href", "/incidents/INC_1");
  });

  it("says the trail cannot be edited or pruned", async () => {
    await show();
    // Its value is that it has no delete path — including from retention.
    expect(screen.getByText(/Append-only/)).toBeInTheDocument();
    expect(screen.getByText(/retention, which leaves this table alone/))
      .toBeInTheDocument();
  });

  it("says plainly when a filter matches nothing", async () => {
    mocked.audit.mockResolvedValue({
      entries: [], matched: 0, next_cursor: null, event_types: [] });
    render(<MemoryRouter><Audit /></MemoryRouter>);

    expect(await screen.findByText(/Nothing in the trail matches that/))
      .toBeInTheDocument();
  });

  it("surfaces a failure rather than an empty table", async () => {
    mocked.audit.mockRejectedValue(new ApiError(500, "boom"));
    render(<MemoryRouter><Audit /></MemoryRouter>);

    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});
