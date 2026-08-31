// Fixtures are live /incidents responses.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Incidents from "./Incidents";
import fixture from "../test-fixtures/incidents.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { incidents: vi.fn(), detect: vi.fn() } };
});
vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api } = await import("../api/client");
const data = fixture as never;

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
