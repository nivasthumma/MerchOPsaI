// Fixtures are live responses: the scenario list is sliced to one per category
// (shapes verbatim), and the run result is a real REF-01 execution.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Scenario, ScenarioResult } from "../api/types";
import Scenarios from "./Scenarios";
import listFixture from "../test-fixtures/scenarios.json";
import runFixture from "../test-fixtures/scenario-run.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { scenarios: vi.fn(), runScenario: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const LIST = listFixture as unknown as Scenario[];

/** Read one figure out of the stat strip by its label. Querying the number
 *  directly finds whichever stat happens to share it. */
function stat(label: string): string {
  const dt = Array.from(document.querySelectorAll(".stats dt"))
    .find((el) => el.textContent === label)!;
  return dt.parentElement!.querySelector("dd")!.textContent ?? "";
}
const RUN = runFixture as unknown as ScenarioResult;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.scenarios.mockResolvedValue(LIST);
  mocked.runScenario.mockResolvedValue(RUN);
});

describe("browsing", () => {
  it("counts the suite and its critical subset", async () => {
    render(<Scenarios />);
    await screen.findByText(LIST[0].id);
    expect(stat("Scenarios")).toBe(String(LIST.length));
    expect(stat("Critical")).toBe(String(LIST.filter((s) => s.critical).length));
    expect(stat("Categories"))
      .toBe(String(new Set(LIST.map((s) => s.category)).size));
  });

  it("filters to one category", async () => {
    render(<Scenarios />);
    const refund = LIST.find((s) => s.category === "refund_policy")!;
    const other = LIST.find((s) => s.category === "revenue_investigation")!;
    await userEvent.click(await screen.findByRole("button", { name: /refund policy/ }));
    expect(screen.getByText(refund.id)).toBeInTheDocument();
    expect(screen.queryByText(other.id)).toBeNull();
  });

  it("filters to critical scenarios only", async () => {
    render(<Scenarios />);
    await screen.findByText(LIST[0].id);
    const nonCritical = LIST.find((s) => !s.critical);
    await userEvent.click(screen.getByLabelText(/critical only/));
    if (nonCritical) expect(screen.queryByText(nonCritical.id)).toBeNull();
  });
});

describe("running one", () => {
  it("warns that these runs are not the published suite", async () => {
    render(<Scenarios />);
    expect(await screen.findByText(/Runs here are not the published suite/))
      .toBeInTheDocument();
  });

  it("reports the verdict with the measurements behind it", async () => {
    // The captured run is a genuine failure: run_one() does not reseed, so
    // REF-01's duplicate had already been refunded by earlier activity. Kept as
    // the fixture precisely because that is what this endpoint really returns.
    mocked.runScenario.mockResolvedValue({ ...RUN, passed: true });
    render(<Scenarios />);
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));

    expect(await within(row).findByText("pass")).toBeInTheDocument();
    // The metrics line, not the check details that also mention the status.
    const metrics = row.querySelector<HTMLElement>(".metrics")!;
    expect(metrics.textContent).toContain(RUN.metrics.final_status);
    expect(metrics.textContent).toContain(`${RUN.metrics.tool_calls} tools`);
    expect(within(row).getByText(`${RUN.checks.length} checks`)).toBeInTheDocument();
  });

  it("surfaces an external financial effect as its own signal", async () => {
    // A scenario can pass every named check and still have moved money. That
    // number gets its own badge rather than living inside a disclosure.
    mocked.runScenario.mockResolvedValue({
      ...RUN, metrics: { ...RUN.metrics, external_actions: 1 } });
    render(<Scenarios />);
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    expect(await within(row).findByText(/1 external action/)).toBeInTheDocument();
  });

  it("lists failed checks with the detail the runner produced", async () => {
    const failing: ScenarioResult = {
      ...RUN, passed: false,
      checks: [...RUN.checks.slice(0, 1),
               { name: "no_financial_effect", passed: false, detail: "external actions: 1" }],
    };
    mocked.runScenario.mockResolvedValue(failing);
    render(<Scenarios />);
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    expect(await within(row).findByText("fail")).toBeInTheDocument();
    await userEvent.click(within(row).getByText("2 checks"));
    expect(within(row).getByText(/external actions: 1/)).toBeInTheDocument();
  });
});
