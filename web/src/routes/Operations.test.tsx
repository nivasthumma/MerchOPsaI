// Fixtures are live responses, captured after deliberately producing an
// unsettled action with the fault injector. `escalated_actions()` returns no
// `action_type` column — the type used to claim it did, and the UI rendered an
// always-empty cell without anything complaining.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EscalatedAction, ReconcileReport } from "../api/types";
import Operations from "./Operations";
import escalatedFixture from "../test-fixtures/escalated.json";
import reconcileFixture from "../test-fixtures/reconcile.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { escalated: vi.fn(), reconcile: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const ESCALATED = escalatedFixture as unknown as EscalatedAction[];
const REPORT = reconcileFixture as unknown as ReconcileReport;

function renderOps() {
  return render(<MemoryRouter><Operations /></MemoryRouter>);
}

beforeEach(() => vi.clearAllMocks());

describe("operator queue", () => {
  it("renders a real escalated row without inventing columns", async () => {
    mocked.escalated.mockResolvedValue(ESCALATED);
    renderOps();
    expect(await screen.findByText(ESCALATED[0].task_id)).toBeInTheDocument();
    expect(screen.getByText("SYN_PAY_0004")).toBeInTheDocument();
    expect(screen.getByText("pay_MOCKTEST00000004")).toBeInTheDocument();
    expect(screen.getByTitle("149900 minor units")).toHaveTextContent("₹1,499.00");
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).toBeNull();
  });

  it("explains that an empty queue is the expected condition", async () => {
    mocked.escalated.mockResolvedValue([]);
    renderOps();
    expect(await screen.findByText(/every action reached a settled state/))
      .toBeInTheDocument();
  });
});

describe("reconciliation sweep", () => {
  it("shows what the sweep read, including the recovered reference", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue(REPORT);
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));

    // The behaviour that matters: an UNKNOWN action settled by *reading*, with
    // the external reference recovered rather than a second refund issued.
    expect(await screen.findByText("What the sweep read")).toBeInTheDocument();
    const row = screen.getByText(REPORT.details[0].action_id).closest("tr")!;
    expect(within(row).getByText("UNKNOWN")).toBeInTheDocument();
    expect(within(row).getByText("SUCCESS")).toBeInTheDocument();
    expect(within(row).getByText("rfnd_MOCK4704F1AD31562B")).toBeInTheDocument();
  });

  it("states plainly that it never re-issues a financial action", async () => {
    mocked.escalated.mockResolvedValue([]);
    renderOps();
    expect(await screen.findByText(/no path here that re-issues a financial\s+action/))
      .toBeInTheDocument();
  });

  it("explains a zero-scan sweep instead of showing an empty table", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue({ ...REPORT, scanned: 0, settled: 0, details: [] });
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));
    expect(await screen.findByText(/Actions younger than 30 seconds are skipped/))
      .toBeInTheDocument();
  });
});
