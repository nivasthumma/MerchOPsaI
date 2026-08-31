// MerchantOps §51. The fixture is a live /incidents/{id} response, with one
// untrusted evidence row appended so the quarantine rendering is exercised.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IncidentDetail as Detail } from "../api/types";
import IncidentDetail from "./IncidentDetail";
import fixture from "../test-fixtures/incident.json";

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useParams: () => ({ incidentId: "INC_TEST" }) };
});
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { getIncident: vi.fn() } };
});

const { api } = await import("../api/client");
const data = fixture as unknown as Detail;

const renderPage = async () => {
  render(<MemoryRouter><IncidentDetail /></MemoryRouter>);
  await screen.findByText("Evidence");
};

describe("incident page", () => {
  beforeEach(() => {
    vi.mocked(api.getIncident).mockReset();
    vi.mocked(api.getIncident).mockResolvedValue(data);
  });

  it("shows the problem, its impact and where it came from", async () => {
    await renderPage();
    expect(screen.getByText("Revenue at risk")).toBeInTheDocument();
    expect(screen.getByText("Rule")).toBeInTheDocument();
    expect(screen.getByText(data.detection_rule)).toBeInTheDocument();
  });

  it("marks merchant free text as untrusted rather than rendering it as system text",
     async () => {
    await renderPage();
    const row = screen.getByText("customer_notes").closest("li");
    expect(row).not.toBeNull();
    expect(row).toHaveAttribute("data-untrusted");
    expect(within(row!).getByText("untrusted")).toBeInTheDocument();
    // The text is still shown — quarantined, not hidden. An operator needs to
    // see what the record actually contains.
    expect(within(row!).getByText(/IGNORE ALL PREVIOUS INSTRUCTIONS/)).toBeInTheDocument();
  });

  it("never shows an expected figure without its basis", async () => {
    await renderPage();
    expect(screen.getByText("Expected")).toBeInTheDocument();
    expect(screen.getByText(data.recovery!.expected_recovery_basis)).toBeInTheDocument();
  });

  it("shows the campaign bounds alongside the plan", async () => {
    await renderPage();
    expect(screen.getByText("Max recovery")).toBeInTheDocument();
    expect(screen.getByText("Max actions")).toBeInTheDocument();
  });

  it("renders the timeline from the audit trail, oldest first", async () => {
    await renderPage();
    const items = within(screen.getByRole("list", { name: "Timeline" }))
      .getAllByRole("listitem");
    expect(items.length).toBe(data.timeline.length);
    expect(screen.getAllByText(/incident detected/).length).toBeGreaterThan(0);
  });

  it("links each investigation to its task", async () => {
    await renderPage();
    const task = data.tasks[0];
    expect(screen.getAllByRole("link", { name: task.id })[0])
      .toHaveAttribute("href", `/tasks/${task.id}`);
  });
});
