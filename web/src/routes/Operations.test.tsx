// Fixtures are live responses, captured after deliberately producing an
// unsettled action with the fault injector. `escalated_actions()` returns no
// `action_type` column — the type used to claim it did, and the UI rendered an
// always-empty cell without anything complaining.

import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
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

function LocationProbe() {
  return <div data-testid="location">{useLocation().search}</div>;
}

function renderOps() {
  return render(<MemoryRouter><Operations /><LocationProbe /></MemoryRouter>);
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
    expect(await screen.findByText(/expected condition, not a missing feature/))
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

describe("queue scope", () => {
  it("asks for the escalation threshold by default", async () => {
    mocked.escalated.mockResolvedValue([]);
    renderOps();
    await screen.findByText(/Nothing escalated/);
    expect(mocked.escalated).toHaveBeenCalledWith(5);
  });

  it("shows work in progress when asked for everything unsettled", async () => {
    // An action UNKNOWN with one attempt is invisible at the escalation
    // threshold — it is precisely what the sweep has not given up on.
    const inProgress = { ...ESCALATED[0], verify_attempts: 1 };
    mocked.escalated.mockResolvedValueOnce([]).mockResolvedValueOnce([inProgress]);
    renderOps();
    await screen.findByText(/Nothing escalated/);

    await userEvent.click(screen.getByRole("button", { name: "All unsettled" }));
    expect(mocked.escalated).toHaveBeenLastCalledWith(0);
    expect(await screen.findByText(inProgress.task_id)).toBeInTheDocument();
  });

  it("says why an empty queue is empty, differently for each scope", async () => {
    mocked.escalated.mockResolvedValue([]);
    renderOps();
    expect(await screen.findByText(/no action has exhausted its attempts/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "All unsettled" }));
    expect(await screen.findByText(/every action reached SUCCESS, FAILED or PARTIAL/))
      .toBeInTheDocument();
  });
});

describe("sweep details", () => {
  it("renders an unrecognised state as itself rather than forcing a pill", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue({
      ...REPORT,
      details: [{ ...REPORT.details[0], to: "SOMETHING_NEW" }],
    });
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));
    const row = (await screen.findByText(REPORT.details[0].action_id)).closest<HTMLElement>("tr")!;
    expect(within(row).getByText("SOMETHING_NEW")).toBeInTheDocument();
  });
});

describe("an operator working the queue", () => {
  const STUCK = {
    ...ESCALATED[0],
    verification_detail: {
      state: "UNKNOWN" as const,
      reason: "Connection lost after the request was submitted. The provider may or may not have applied it.",
    },
  };

  it("says why an action is stuck, on the row", async () => {
    // A queue of identifiers is a lookup exercise: without this an operator
    // opens every task to find out what happened.
    mocked.escalated.mockResolvedValue([STUCK]);
    renderOps();
    expect(await screen.findByText(/Connection lost after the request was submitted/))
      .toBeInTheDocument();
  });

  it("keeps itself current, and says when it last read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocked.escalated.mockResolvedValue([STUCK]);
    renderOps();
    await screen.findByText(STUCK.task_id);
    expect(screen.getByText("live")).toBeInTheDocument();

    const before = mocked.escalated.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(31000); });
    // This list changes without anyone touching the tab — a cron sweep, another
    // operator. Stale is worse than empty, because stale looks current.
    expect(mocked.escalated.mock.calls.length).toBeGreaterThan(before);
    vi.useRealTimers();
  });

  it("uses the endpoint's own guard by default", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue(REPORT);
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));
    expect(mocked.reconcile).toHaveBeenCalledWith({ minAgeSeconds: 30 });
  });

  it("marks the guard as relaxed when it is lowered", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue(REPORT);
    renderOps();
    await userEvent.click(await screen.findByText(/Minimum age/));
    await userEvent.click(screen.getByRole("button", { name: "0s" }));
    // Lowering it burns attempts on healthy actions; that should never be a
    // silent setting.
    expect(screen.getByText("guard relaxed")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Run sweep" }));
    expect(mocked.reconcile).toHaveBeenCalledWith({ minAgeSeconds: 0 });
  });
});

describe("following up on what the sweep did", () => {
  it("links a swept action to its task", async () => {
    // "ACT_x settled UNKNOWN -> SUCCESS" is a statement an operator cannot act
    // on without the task. The reconciler knew it; the report did not carry it.
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue({
      ...REPORT,
      details: [{ ...REPORT.details[0], task_id: "TASK_ABC123" }],
    });
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));
    const link = await screen.findByRole("link", { name: REPORT.details[0].action_id });
    expect(link).toHaveAttribute("href", "/tasks/TASK_ABC123");
  });

  it("still renders a row whose task is unknown", async () => {
    mocked.escalated.mockResolvedValue([]);
    mocked.reconcile.mockResolvedValue({
      ...REPORT, details: [{ ...REPORT.details[0], task_id: null }] });
    renderOps();
    await userEvent.click(await screen.findByRole("button", { name: "Run sweep" }));
    expect(await screen.findByText(REPORT.details[0].action_id)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: REPORT.details[0].action_id })).toBeNull();
  });

  it("puts the queue scope in the URL, like every other view in the app", async () => {
    mocked.escalated.mockResolvedValue([]);
    renderOps();
    await screen.findByText(/Nothing escalated/);
    await userEvent.click(screen.getByRole("button", { name: "All unsettled" }));
    expect(screen.getByTestId("location")).toHaveTextContent("scope=all");
    expect(mocked.escalated).toHaveBeenLastCalledWith(0);
  });
});
