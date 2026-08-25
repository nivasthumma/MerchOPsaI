// The fixture is a real "Why did revenue drop this week?" response: 13
// findings, 12 of them measured, four of those duplicated across two tool
// calls, and one whose value is a list. Every one of those shapes is a way to
// render something wrong.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../api/types";
import Investigate from "./Investigate";
import taskFixture from "../test-fixtures/investigate.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { createTask: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const TASK = taskFixture as unknown as Task;

function renderPage() {
  return render(<MemoryRouter><Investigate /></MemoryRouter>);
}

async function investigate() {
  await userEvent.click(screen.getByRole("button", { name: "Why did revenue drop this week?" }));
  await userEvent.click(screen.getByRole("button", { name: "Investigate" }));
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mocked.createTask.mockResolvedValue(TASK);
});

describe("asking", () => {
  it("sends the question and shows the narrative answer", async () => {
    renderPage();
    await investigate();
    expect(mocked.createTask).toHaveBeenCalledWith("Why did revenue drop this week?");
    expect(await screen.findByText(/Revenue moved -6.29%/)).toBeInTheDocument();
  });

  it("will not submit an empty question", () => {
    renderPage();
    expect(screen.getByRole("button", { name: "Investigate" })).toBeDisabled();
  });
});

describe("evidence", () => {
  it("shows one row per metric, not one per citation", async () => {
    renderPage();
    await investigate();
    await screen.findByText(/Revenue moved/);

    // card_success_change_pp is reported by two different tool calls. That is
    // one fact with two citations, not two findings.
    const rows = screen.getAllByRole("row").filter(
      (r) => within(r).queryByText("card_success_change_pp"));
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText(/TC_.*, TC_/)).toBeInTheDocument();
  });

  it("formats a list-valued metric instead of concatenating it", async () => {
    renderPage();
    await investigate();
    const row = (await screen.findByText("upi_worst_hours")).closest<HTMLElement>("tr")!;
    // Rendering the array directly would run the entries together.
    expect(within(row).getByText(/20:00 \(75.0% failed\), /)).toBeInTheDocument();
  });

  it("reports grounding as a count rather than a claim", async () => {
    renderPage();
    await investigate();
    await screen.findByText(/Revenue moved/);
    const observed = (TASK.findings ?? []).filter((f) => f.kind === "OBSERVED");
    const dt = Array.from(document.querySelectorAll(".stats dt"))
      .find((el) => el.textContent === "Grounded")!;
    expect(dt.parentElement!.querySelector("dd")!.textContent)
      .toBe(`${observed.length}/${observed.length}`);
  });

  it("does not print the conclusion twice", async () => {
    // The agent's concluding finding carries the same prose as final_answer.
    // Rendering both put the identical paragraph on the page twice.
    renderPage();
    await investigate();
    await screen.findByText(/Revenue moved -6.29%/);
    expect(screen.getAllByText(/Revenue moved -6.29%/)).toHaveLength(1);
  });

  it("shows what the conclusion is grounded in", async () => {
    renderPage();
    await investigate();
    expect(await screen.findByText(/inferred · grounded in TC_/)).toBeInTheDocument();
  });
});

describe("navigation", () => {
  it("links to the task in-app rather than reloading the page", async () => {
    renderPage();
    await investigate();
    const link = await screen.findByRole("link", { name: /Open the full trace/ });
    // A plain <a href> would drop the SPA and re-fetch everything.
    expect(link).toHaveAttribute("href", `/tasks/${TASK.id}`);
  });

  it("remembers recent tasks in this browser only", async () => {
    renderPage();
    await investigate();
    expect(JSON.parse(localStorage.getItem("merchantops.recent")!)[0].id).toBe(TASK.id);
  });
});
