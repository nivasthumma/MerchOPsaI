// The fixture is a live `/events` response, captured after a detection sweep
// and an investigation — thirteen frames across seven of §62's fifteen types.
// Inventing one would have let the summariser assume payload fields the server
// does not actually send.

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveEventList } from "../api/types";
import Timeline from "./Timeline";
import eventsFixture from "../test-fixtures/events.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { events: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const PAGE = eventsFixture as unknown as LiveEventList;
const EMPTY: LiveEventList = { events: [], next_cursor: null, pending: 0 };

const renderTimeline = () =>
  render(<MemoryRouter><Timeline /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());

describe("the live timeline", () => {
  it("renders the frames the server actually sent", async () => {
    mocked.events.mockResolvedValue(PAGE);
    renderTimeline();

    // Not a template: these names come out of the fixture. Plural queries
    // because the sweep raised three incidents, so `incident.created` appears
    // three times — a singular query would be asserting the fixture had one.
    expect((await screen.findAllByText("incident.created")).length)
      .toBe(PAGE.events.filter((e) => e.event === "incident.created").length);
    expect(screen.getAllByText("tool.completed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("hypothesis.rejected").length).toBeGreaterThan(0);
  });

  it("asks for what came after the last id it saw, and appends", async () => {
    // The whole point of a cursor: the second poll must not re-request the
    // frames already on screen, and must not replace them either.
    const first = PAGE.events.slice(0, 2);
    const second = PAGE.events.slice(2, 4);
    mocked.events
      .mockResolvedValueOnce({ events: first, next_cursor: first[1].id, pending: 0 })
      .mockResolvedValue({ events: second, next_cursor: second[1].id, pending: 0 });

    renderTimeline();
    await screen.findAllByText(first[0].event);

    expect(mocked.events).toHaveBeenLastCalledWith(null);

    // Drive the second poll directly rather than waiting out the interval:
    // becoming visible re-polls, which is the same code path the timer uses.
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await screen.findAllByText(second[0].event);
    // The cursor moved, and the earlier frames are still there.
    expect(mocked.events).toHaveBeenLastCalledWith(first[1].id);
    expect(screen.getAllByText(first[0].event).length).toBeGreaterThan(0);
  });

  it("shows undelivered frames rather than looking quiet", async () => {
    // A drain that has stopped is invisible from the frames alone: the
    // timeline just stops moving, which reads as a calm system.
    mocked.events.mockResolvedValue({ ...PAGE, pending: 13 });
    renderTimeline();

    // The count is rendered with a title explaining what "undelivered" means,
    // because a bare number on a dashboard invites the wrong reading.
    const count = await screen.findByTitle(/not yet delivered/i);
    expect(count).toHaveTextContent("13");
  });

  it("says why it is empty when frames are stuck undelivered", async () => {
    mocked.events.mockResolvedValue({ events: [], next_cursor: null, pending: 7 });
    renderTimeline();
    expect(await screen.findByText(/7 frame\(s\) are waiting on the drain/))
      .toBeInTheDocument();
  });

  it("distinguishes an empty system from a stuck one", async () => {
    mocked.events.mockResolvedValue(EMPTY);
    renderTimeline();
    expect(await screen.findByText(/No activity yet/)).toBeInTheDocument();
  });

  it("filters to one incident without refetching", async () => {
    mocked.events.mockResolvedValue(PAGE);
    renderTimeline();
    await screen.findAllByText("incident.created");

    const withIncident = PAGE.events.find((e) => e.incident_id);
    expect(withIncident, "fixture has no incident-linked frame").toBeTruthy();

    const callsBefore = mocked.events.mock.calls.length;
    await userEvent.click(screen.getAllByRole("button", { name: "only this" })[0]);

    expect(await screen.findByText(/Showing/)).toBeInTheDocument();
    // Filtering is a view over what is already loaded; it must not re-query.
    expect(mocked.events.mock.calls.length).toBe(callsBefore);
  });

  it("renders an unfamiliar event type rather than dropping it", async () => {
    // The vocabulary is closed server-side, so a name this app does not know
    // means the app is behind. Hiding it would hide that.
    const odd = { ...PAGE.events[0], id: "EVT_ODD", event: "something.new" };
    mocked.events.mockResolvedValue({ events: [odd], next_cursor: "EVT_ODD", pending: 0 });
    renderTimeline();
    expect(await screen.findByText("something.new")).toBeInTheDocument();
  });

  it("surfaces a failed poll instead of showing a frozen list", async () => {
    mocked.events.mockRejectedValue(
      Object.assign(new Error("boom"), { status: 500, detail: "boom" }));
    renderTimeline();
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});
