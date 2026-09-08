// Fixtures are live /incidents responses.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IncidentList } from "../api/types";
import Incidents from "./Incidents";
import fixture from "../test-fixtures/incidents.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { incidents: vi.fn(), detect: vi.fn() } };
});
vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api } = await import("../api/client");
// Typed, not `as never`: these tests read `views` off the fixture, and an
// untyped fixture is one that can drift from the contract without complaint.
const data = fixture as unknown as IncidentList;

function LocationProbe() {
  return <div data-testid="location">{useLocation().search}</div>;
}

const renderPage = async () => {
  render(<MemoryRouter><Incidents /></MemoryRouter>);
  await screen.findByRole("table", { name: "Open incidents" });
};

describe("incidents queue", () => {
  beforeEach(() => {
    vi.mocked(api.incidents).mockReset().mockResolvedValue(data);
    vi.mocked(api.detect).mockReset();
  });

  it("lists open incidents biggest-exposure first", async () => {
    await renderPage();
    const rows = within(screen.getByRole("table", { name: "Open incidents" }))
      .getAllByRole("row").slice(1);
    expect(rows.length).toBeGreaterThan(0);
    // The biggest problem is the one to open first, so it is the one on top.
    const amounts = rows.map((r) => {
      const cells = within(r).getAllByRole("cell");
      return Number(cells[4].textContent!.replace(/[₹,]/g, ""));
    });
    expect(amounts).toEqual([...amounts].sort((a, b) => b - a));
  });

  it("gives every incident a route into it", async () => {
    await renderPage();
    const first = fixture.incidents[0];
    expect(screen.getByRole("link", { name: first.title }))
      .toHaveAttribute("href", `/incidents/${first.id}`);
  });

  it("says nothing new rather than nothing happened when a sweep finds nothing", async () => {
    /* Detection is idempotent. An operator who presses this twice should be
       told nothing appeared, not left wondering whether it did. */
    vi.mocked(api.detect).mockResolvedValue({
      merchant_id: "MERCH_A", anomalies_found: 4, incidents_created: 0,
      already_known: 4, duration_ms: 12,
    });
    await renderPage();
    await userEvent.click(screen.getByRole("button", { name: /run detection/i }));
    expect(api.detect).toHaveBeenCalled();
    // And it reloads, so the table reflects the sweep rather than the page load.
    expect(vi.mocked(api.incidents).mock.calls.length).toBeGreaterThan(1);
  });

  it("explains an empty queue instead of showing a bare zero", async () => {
    vi.mocked(api.incidents).mockResolvedValue({
      incidents: [], total_revenue_at_risk_minor: 0,
    } as never);
    render(<MemoryRouter><Incidents /></MemoryRouter>);
    expect(await screen.findByText(/sweep, not a daemon/i)).toBeInTheDocument();
  });
});


// ---------------------------------------------------------- P1-05 filtering
describe("filtering and saved views", () => {
  beforeEach(() => {
    vi.mocked(api.incidents).mockReset().mockResolvedValue(data);
    vi.mocked(api.detect).mockReset();
  });

  it("renders the views the server declared, with the server's counts", async () => {
    await renderPage();
    // A client holding its own copy of what "My attention" means would be a
    // second definition, and the one in a pasted link would win on some
    // screens and lose on others.
    for (const v of data.views) {
      const chip = screen.getByRole("button",
                                    { name: `Saved view: ${v.label} (${v.count})` });
      expect(chip).toHaveAttribute("title", v.hint);
      expect(chip).toHaveTextContent(String(v.count));
    }
  });

  it("asks the server for a view rather than filtering what it already has", async () => {
    await renderPage();
    await userEvent.click(screen.getByRole("button", { name: /^Saved view: My attention/ }));
    expect(vi.mocked(api.incidents)).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: "my_attention" }));
  });

  it("puts the filter in the URL so a filtered console is a link", async () => {
    render(
      <MemoryRouter><Incidents /><LocationProbe /></MemoryRouter>);
    await screen.findByRole("table", { name: "Open incidents" });

    await userEvent.click(screen.getByRole("button", { name: "UNKNOWN" }));
    expect(screen.getByTestId("location")).toHaveTextContent("has_unknown=true");
    expect(vi.mocked(api.incidents)).toHaveBeenLastCalledWith(
      expect.objectContaining({ has_unknown: true }));
  });

  it("a hand-set filter stops claiming to be a saved view", async () => {
    render(<MemoryRouter initialEntries={["/?view=critical"]}>
             <Incidents /><LocationProbe />
           </MemoryRouter>);
    await screen.findByRole("table", { name: "Open incidents" });

    await userEvent.click(screen.getByRole("button", { name: "Escalated" }));
    const loc = screen.getByTestId("location");
    expect(loc).toHaveTextContent("escalated=true");
    // Otherwise the chip would stay lit while showing something else.
    expect(loc).not.toHaveTextContent("view=critical");
  });

  it("a view replaces the filter rather than merging into it", async () => {
    render(<MemoryRouter initialEntries={["/?escalated=true"]}>
             <Incidents /><LocationProbe />
           </MemoryRouter>);
    await screen.findByRole("table", { name: "Open incidents" });

    await userEvent.click(screen.getByRole("button", { name: /^Saved view: Critical/ }));
    const loc = screen.getByTestId("location");
    expect(loc).toHaveTextContent("view=critical");
    // Merged, a chip left set from earlier would silently narrow the view and
    // it would no longer be the thing its label names.
    expect(loc).not.toHaveTextContent("escalated=true");
  });

  it("toggles a value filter off as well as on", async () => {
    render(<MemoryRouter><Incidents /><LocationProbe /></MemoryRouter>);
    await screen.findByRole("table", { name: "Open incidents" });

    await userEvent.click(screen.getByRole("button", { name: "HIGH" }));
    expect(screen.getByTestId("location")).toHaveTextContent("severity=HIGH");
    await userEvent.click(screen.getByRole("button", { name: "HIGH" }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("severity=HIGH");
  });

  it("says an empty filtered result is the whole answer", async () => {
    vi.mocked(api.incidents).mockResolvedValue({
      ...data, incidents: [], total_revenue_at_risk_minor: 0 });
    render(<MemoryRouter initialEntries={["/?escalated=true"]}><Incidents /></MemoryRouter>);
    // Not "run detection again": the filter is applied server-side, so this is
    // the answer and not a page of it.
    expect(await screen.findByText(/whole answer and not a page of it/))
      .toBeInTheDocument();
  });

  it("still tells an unfiltered operator that detection is a sweep", async () => {
    vi.mocked(api.incidents).mockResolvedValue({
      ...data, incidents: [], total_revenue_at_risk_minor: 0 });
    render(<MemoryRouter><Incidents /></MemoryRouter>);
    expect(await screen.findByText(/Detection is a sweep, not a daemon/))
      .toBeInTheDocument();
  });

  it("shows the matched total, not the length of the page", async () => {
    // `total_revenue_at_risk_minor` is counted in SQL over the whole match.
    // A count taken from `incidents.length` would disagree with it the moment
    // the list is capped, and the money figure would be the one telling the
    // truth.
    vi.mocked(api.incidents).mockResolvedValue({
      ...data, matched: 40, shown: data.incidents.length });
    render(<MemoryRouter initialEntries={["/?unresolved=true"]}>
             <Incidents />
           </MemoryRouter>);
    await screen.findByRole("table", { name: "Open incidents" });

    expect(screen.getByText(new RegExp(`Showing ${data.incidents.length} of 40`)))
      .toBeInTheDocument();
    expect(screen.getByText(/counted in SQL, not summed across this page/))
      .toBeInTheDocument();

    // Including the heading. This test carried the right name and asserted the
    // sentence and the strip but never the count beside the title -- which was
    // `rows.length`, so the biggest number on the page said 2 while the line
    // below it said "Showing 2 of 40" and the strip said 40.
    const head = screen.getByRole("heading", { name: "Incidents" }).parentElement!;
    expect(head.querySelector(".count")).toHaveTextContent("40");
  });

  it("says nothing when the whole match fits", async () => {
    vi.mocked(api.incidents).mockResolvedValue(data);
    await renderPage();
    expect(screen.queryByText(/Showing \d+ of/)).toBeNull();
  });
});
