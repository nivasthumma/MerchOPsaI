// Fixtures are live responses: the scenario list is sliced to one per category
// (shapes verbatim), and the run result is a real REF-01 execution.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Scenario, ScenarioResult } from "../api/types";
import Scenarios from "./Scenarios";
import listFixture from "../test-fixtures/scenarios-full.json";
import runFixture from "../test-fixtures/scenario-run.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { scenarios: vi.fn(), runScenario: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const ALL = listFixture as unknown as Scenario[];
// One per category — including REF-01, which the run fixture belongs to — plus
// two non-critical rows so the critical filter has something to hide.
const FIRST_OF_CATEGORY = Object.values(
  ALL.reduce<Record<string, Scenario>>((acc, s) => {
    acc[s.category] ??= s;
    return acc;
  }, {}));
const LIST = [...FIRST_OF_CATEGORY,
              ...ALL.filter((s) => !s.critical && !FIRST_OF_CATEGORY.includes(s)).slice(0, 2)];

/** MemoryRouter never touches window.location, so a URL assertion reads the
 *  router's own location instead. */
function LocationProbe() {
  return <div data-testid="location">{useLocation().search}</div>;
}

function renderPage() {
  return render(<MemoryRouter><Scenarios /><LocationProbe /></MemoryRouter>);
}

/** The controls render only once the list has arrived. */
const search = () => screen.findByLabelText("Search scenarios");

/** Read one figure out of the stat strip by its label. Querying the number
 *  directly finds whichever stat happens to share it. */
function stat(label: string): string {
  const dt = Array.from(document.querySelectorAll(".stats dt"))
    .find((el) => el.textContent === label)!;
  return dt.parentElement!.querySelector("dd")!.textContent ?? "";
}
const RUN = runFixture as unknown as ScenarioResult;

beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  mocked.scenarios.mockResolvedValue(LIST);
  mocked.runScenario.mockResolvedValue(RUN);
});

describe("browsing", () => {
  it("counts the suite and its critical subset", async () => {
    renderPage();
    await screen.findByText(LIST[0].id);
    expect(stat("Scenarios")).toBe(String(LIST.length));
    expect(stat("Critical")).toBe(String(LIST.filter((s) => s.critical).length));
    expect(stat("Categories"))
      .toBe(String(new Set(LIST.map((s) => s.category)).size));
  });

  it("filters to one category", async () => {
    renderPage();
    const refund = LIST.find((s) => s.category === "refund_policy")!;
    const other = LIST.find((s) => s.category === "revenue_investigation")!;
    await userEvent.click(await screen.findByRole("button", { name: /refund policy/ }));
    expect(screen.getByText(refund.id)).toBeInTheDocument();
    expect(screen.queryByText(other.id)).toBeNull();
  });

  it("filters to critical scenarios only", async () => {
    renderPage();
    await screen.findByText(LIST[0].id);
    const nonCritical = LIST.find((s) => !s.critical);
    await userEvent.click(screen.getByLabelText(/critical only/));
    if (nonCritical) expect(screen.queryByText(nonCritical.id)).toBeNull();
  });
});

describe("running one", () => {
  it("warns that these runs are not the published suite", async () => {
    renderPage();
    expect(await screen.findByText(/Runs here are not the published suite/))
      .toBeInTheDocument();
  });

  it("reports the verdict with the measurements behind it", async () => {
    // The captured run is a genuine failure: run_one() does not reseed, so
    // REF-01's duplicate had already been refunded by earlier activity. Kept as
    // the fixture precisely because that is what this endpoint really returns.
    mocked.runScenario.mockResolvedValue({ ...RUN, passed: true });
    renderPage();
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
    renderPage();
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
    renderPage();
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    expect(await within(row).findByText("fail")).toBeInTheDocument();
    await userEvent.click(within(row).getByText("2 checks"));
    expect(within(row).getByText(/external actions: 1/)).toBeInTheDocument();
  });
});

describe("what a scenario actually asserts", () => {
  it("renders the assertions as sentences, not as JSON", async () => {
    renderPage();
    const sec = ALL.find((s) => s.id === "SEC-01")!;
    await userEvent.type(await search(), "SEC-01");
    const row = (await screen.findByText(sec.id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByText(/what it asserts/));

    // SEC-01 expects: 0 external calls, no financial effect, and an answer that
    // never repeats what the injected text asked for.
    expect(within(row).getByText("makes 0 external calls")).toBeInTheDocument();
    expect(within(row).getByText("moves no money")).toBeInTheDocument();
    expect(within(row).getByText(/answer never mentions/)).toBeInTheDocument();
  });

  it("shows the request and the principal, because they change the meaning", async () => {
    renderPage();
    const analyst = ALL.find((s) => s.principal === "analyst")!;
    await userEvent.type(await search(), analyst.id);
    const row = (await screen.findByText(analyst.id)).closest<HTMLElement>("tr")!;
    // Visible without expanding: an analyst scenario is a different scenario.
    expect(within(row).getByText("as analyst")).toBeInTheDocument();
    await userEvent.click(within(row).getByText(/what it asserts/));
    expect(within(row).getByText(`“${analyst.request}”`)).toBeInTheDocument();
  });

  it("describes injected setup in words", async () => {
    renderPage();
    const faulty = ALL.find((s) => (s.setup as { fault?: unknown }).fault)!;
    await userEvent.type(await search(), faulty.id);
    const row = (await screen.findByText(faulty.id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByText(/what it asserts/));
    expect(within(row).getByText(/injects TIMEOUT_AFTER_SUBMIT on create_refund/))
      .toBeInTheDocument();
  });
});

describe("working through a run", () => {
  it("keeps filters in the URL so a filtered view can be shared", async () => {
    renderPage();
    await screen.findByText(LIST[0].id);
    await userEvent.click(screen.getByRole("button", { name: /refund policy/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("category=refund_policy");

    await userEvent.click(screen.getByLabelText(/critical only/));
    expect(screen.getByTestId("location")).toHaveTextContent("critical=1");
  });

  it("survives a reload without losing a long run", async () => {
    const { unmount } = renderPage();
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    await within(row).findByText("fail");

    // Re-mounting is what a reload does to this component.
    unmount();
    renderPage();
    const again = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    expect(within(again).getByText("fail")).toBeInTheDocument();
  });

  it("floats failures to the top, where they get seen", async () => {
    renderPage();
    const row = (await screen.findByText(RUN.scenario_id)).closest<HTMLElement>("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    await within(row).findByText("fail");

    const ids = screen.getAllByRole("row").slice(1)
      .map((r) => r.querySelector("td")?.textContent ?? "");
    expect(ids[0]).toContain(RUN.scenario_id);
  });
});
