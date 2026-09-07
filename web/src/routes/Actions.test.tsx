// The Action Center — plan P0-03, P0-04, P1-04.
//
// Fixtures are live responses, captured against the seeded database after
// deliberately producing each state: a gated approval, an action left UNKNOWN
// by the timeout fault, and one the repair pass escalated. A fixture written by
// hand is a fixture that can disagree with the API it claims to describe.

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionCenter } from "../api/types";
import Actions from "./Actions";
import awaiting from "../test-fixtures/action-center-awaiting.json";
import unknown from "../test-fixtures/action-center.json";
import escalated from "../test-fixtures/action-center-escalated.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { actionCenter: vi.fn(), reverify: vi.fn() } };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const AWAITING = awaiting as unknown as ActionCenter;
const UNKNOWN = unknown as unknown as ActionCenter;
const ESCALATED = escalated as unknown as ActionCenter;

function renderActions(initial = "/actions") {
  return render(
    <MemoryRouter initialEntries={[initial]}><Actions /></MemoryRouter>);
}

beforeEach(() => vi.clearAllMocks());

describe("the five sections", () => {
  it("renders every section the server declares, in the server's order", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();

    const headings = await screen.findAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent?.replace(/\d+$/, "").trim()))
      .toEqual(["Awaiting approval", "Executing", "Unknown", "Escalated",
                "Recently completed"]);
  });

  it("puts an action in exactly one section", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();

    const id = UNKNOWN.unknown[0].id;
    // A row in two places is a row somebody will action twice.
    expect(await screen.findAllByText(id)).toHaveLength(1);
  });

  it("shows an escalated action as escalated, never as work in progress", async () => {
    mocked.actionCenter.mockResolvedValue(ESCALATED);
    renderActions();

    // It is still UNKNOWN. Listing it under "we are working on it" is the
    // precise misreport the escalated section exists to prevent.
    const row = await screen.findByText(ESCALATED.escalated[0].id);
    expect(row).toBeInTheDocument();
    expect(screen.getByText(/Automatic reconciliation is exhausted/)).toBeInTheDocument();
  });
});

describe("the UNKNOWN queue is work, not a status list", () => {
  it("shows attempts against the system's own limit", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();

    const row = UNKNOWN.unknown[0];
    // Not a hard-coded 5 in the client: the limit comes from the response, so
    // changing the stopping rule server-side cannot leave the UI lying.
    expect(await screen.findByText(
      `${row.verify_attempts} / ${UNKNOWN.reconciliation_policy.max_attempts}`))
      .toBeInTheDocument();
  });

  it("shows the next retry, because P0-04 requires one", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();
    await screen.findByText(UNKNOWN.unknown[0].id);

    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toContain("Next retry");
    expect(headers).toContain("Last check");
    expect(headers).toContain("Attempts");
  });

  it("names the provider and the environment the action was placed in", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();
    // An operator reconciling by hand needs to know which dashboard to open,
    // and a test-mode id and a live id are the same string shape.
    expect(await screen.findByText(/razorpay test/)).toBeInTheDocument();
  });

  it("says an absent provider reference is itself the reason", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();
    await screen.findByText(UNKNOWN.unknown[0].id);
    expect(screen.getByTitle(/No provider reference was issued/)).toBeInTheDocument();
  });

  it("re-verifies through the endpoint and never marks the row resolved itself", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    mocked.reverify.mockResolvedValue({ task: {}, verification: {} });
    renderActions();

    await userEvent.click(await screen.findByRole("button", { name: "Reverify" }));
    expect(mocked.reverify).toHaveBeenCalledWith(UNKNOWN.unknown[0].task_id);
    // The row still reads UNKNOWN: this client renders what the server says
    // and never predicts an outcome (P1-14, no optimistic financial success).
    expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
  });
});

describe("the approval queue", () => {
  it("lists an approval as an approval, not as an action", async () => {
    mocked.actionCenter.mockResolvedValue(AWAITING);
    renderActions();

    const row = AWAITING.awaiting_approval[0];
    expect(await screen.findByText(row.approval_id)).toBeInTheDocument();
    expect(screen.getByText(`0 / ${row.required_signatures}`)).toBeInTheDocument();
  });

  it("sends the reviewer to the evidence rather than offering an inline approve", async () => {
    mocked.actionCenter.mockResolvedValue(AWAITING);
    renderActions();
    await screen.findByText(AWAITING.awaiting_approval[0].approval_id);

    // Approving from a list, with the evidence off screen, is the frictionless
    // financial action this system exists to prevent.
    expect(screen.queryByRole("button", { name: /^Approve$/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Review evidence/ }))
      .toHaveAttribute("href", `/tasks/${AWAITING.awaiting_approval[0].task_id}`);
  });
});

describe("focus and the drawer", () => {
  it("shows only one section when the URL names one", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions("/actions?section=unknown");

    const headings = await screen.findAllByRole("heading", { level: 3 });
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toMatch(/Unknown/);
  });

  it("opens a drawer carrying what P1-04 requires", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();

    const row = UNKNOWN.unknown[0];
    await userEvent.click(await screen.findByRole("button",
                                                  { name: `Open action ${row.id}` }));
    const drawer = screen.getByRole("dialog");
    for (const label of ["Amount", "Payment", "Customer", "Provider",
                         "Provider reference", "Approval", "Attempts",
                         "Last checked", "Next retry"]) {
      expect(within(drawer).getByText(label)).toBeInTheDocument();
    }
    // The one sentence that must be on this surface: re-verify is a read.
    expect(within(drawer).getByText(/never re-sends the action/)).toBeInTheDocument();
  });
});

describe("empty states say what to do next", () => {
  it("explains an empty section rather than printing 'no rows'", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();
    expect(await screen.findByText(/Nothing is waiting on a human decision/))
      .toBeInTheDocument();
    expect(screen.getByText(/Automatic reconciliation has settled everything/))
      .toBeInTheDocument();
  });
});


// ------------------------------------------------------------------ P1-12
describe("the drawer keeps the promise `aria-modal` makes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
  });

  async function openDrawer() {
    renderActions();
    const opener = await screen.findByRole(
      "button", { name: `Open action ${UNKNOWN.unknown[0].id}` });
    await userEvent.click(opener);
    return opener;
  }

  it("moves focus into the panel rather than leaving it on the row behind", async () => {
    await openDrawer();
    const dialog = screen.getByRole("dialog");
    // The panel itself, not its first button: the first control is Close, and
    // landing there announces "close" before saying what was opened.
    expect(document.activeElement).toBe(dialog);
  });

  it("closes on Escape", async () => {
    await openDrawer();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("returns focus to whatever opened it", async () => {
    const opener = await openDrawer();
    await userEvent.keyboard("{Escape}");
    // Without this, closing drops focus onto <body> and the next Tab starts
    // again from the top of the page.
    expect(document.activeElement).toBe(opener);
  });

  it("does not let Tab walk out of a dialog that says it is modal", async () => {
    await openDrawer();
    const dialog = screen.getByRole("dialog");
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input, [tabindex]:not([tabindex="-1"])'));
    expect(focusable.length).toBeGreaterThan(1);

    focusable[focusable.length - 1].focus();
    await userEvent.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);

    focusable[0].focus();
    await userEvent.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe("tables say what they are", () => {
  beforeEach(() => vi.clearAllMocks());

  it("names the action queue and the approval queue differently", async () => {
    mocked.actionCenter.mockResolvedValue(UNKNOWN);
    renderActions();
    expect(await screen.findByRole("table", { name: "Actions" }))
      .toBeInTheDocument();

    cleanup();
    mocked.actionCenter.mockResolvedValue(AWAITING);
    renderActions();
    expect(await screen.findByRole(
      "table", { name: "Approvals awaiting a decision" })).toBeInTheDocument();
  });
});
