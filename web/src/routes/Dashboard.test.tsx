// MerchantOps §49/§50. The fixture is a live /dashboard response.
//
// What these assert is not that numbers render. It is that the page cannot be
// read as saying the money at risk came back — the specific misreading §49
// ends by warning against.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Dashboard as DashboardData } from "../api/types";
import Dashboard from "./Dashboard";
import fixture from "../test-fixtures/dashboard.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { dashboard: vi.fn() } };
});

const { api } = await import("../api/client");
const data = fixture as unknown as DashboardData;

const renderPage = async () => {
  render(<MemoryRouter><Dashboard /></MemoryRouter>);
  await screen.findByText("Revenue recovery");
};

describe("recovery ledger", () => {
  beforeEach(() => {
    vi.mocked(api.dashboard).mockReset();
    vi.mocked(api.dashboard).mockResolvedValue(data);
  });

  it("shows the four figures in narrowing order", async () => {
    await renderPage();
    const chain = screen.getByLabelText("Recovery ledger");
    const labels = within(chain).getAllByRole("term").map((n) => n.textContent);
    expect(labels).toEqual(["Revenue at risk", "Recoverable", "Attempted", "Recovered"]);
  });

  it("does not present money at risk as money recovered", async () => {
    await renderPage();
    const chain = screen.getByLabelText("Recovery ledger");
    const items = within(chain).getAllByRole("listitem");
    const atRisk = items[0].textContent ?? "";
    const recovered = items[3].textContent ?? "";
    expect(atRisk).toContain("35,759.06");
    // Nothing has been recovered in this fixture, and the page must say zero
    // rather than borrowing the at-risk figure.
    expect(recovered).toContain("0.00");
    expect(recovered).not.toContain("35,759.06");
  });

  it("states the basis the figures are measured on", async () => {
    await renderPage();
    expect(screen.getByText(/attributed exposure/i)).toBeInTheDocument();
    expect(screen.getByText(/never when it is merely sent/i)).toBeInTheDocument();
  });

  it("keeps unknown as its own outcome", async () => {
    await renderPage();
    // Not folded into failed or recovered: §33 keeps the size of what we do
    // not know visible.
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("Outstanding")).toBeInTheDocument();
  });

  it("warns loudly when the figures do not nest", async () => {
    vi.mocked(api.dashboard).mockResolvedValue({
      ...data,
      recovery: { ...data.recovery, invariants_broken: ["attempted exceeds recoverable"] },
    });
    await renderPage();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/do not nest/i);
    expect(alert).toHaveTextContent(/attempted exceeds recoverable/);
    // The numbers are still rendered. A dashboard that refuses to draw is one
    // nobody can use to find out why.
    expect(screen.getByLabelText("Recovery ledger")).toBeInTheDocument();
  });

  it("breaks at-risk down by incident and by method", async () => {
    await renderPage();
    expect(screen.getByRole("link", { name: /UPI payment degradation/ }))
      .toHaveAttribute("href", "/incidents/INC_AAA");
    expect(screen.getByText("upi")).toBeInTheDocument();
    expect(screen.getByText("card")).toBeInTheDocument();
  });

  it("reports incident counts and agent activity", async () => {
    await renderPage();
    expect(screen.getByText("Investigations")).toBeInTheDocument();
    expect(screen.getByText("Escalations")).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
  });
});
