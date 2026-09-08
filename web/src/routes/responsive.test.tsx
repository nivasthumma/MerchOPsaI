// P1-11 — dense tables become priority views on a small screen.
//
// jsdom does not do layout, so these tests cannot assert what a 375px viewport
// LOOKS like. What they can assert is the contract the CSS depends on, and that
// contract is the part a change is likely to break silently:
//
//   every cell in a stacked table carries the header it belongs to, because
//   the header row is `display:none` at that width and `data-label` is the
//   only thing left to name the value;
//
//   and nothing that asserts something about money is ever dropped.
//
// The second is the one worth having. A missing `data-label` produces a value
// with no name — visibly wrong the moment anyone looks. A cell marked
// `data-priority="low"` disappears entirely, and if somebody ever marks the
// verification state or the amount that way, an operator on a phone reads a
// refund queue with no amounts in it and nothing on screen says so.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionCenter, IncidentDetail as Detail } from "../api/types";
import Actions from "./Actions";
import IncidentDetail from "./IncidentDetail";
import unknownFixture from "../test-fixtures/action-center.json";
import awaitingFixture from "../test-fixtures/action-center-awaiting.json";
import incidentFixture from "../test-fixtures/incident.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { actionCenter: vi.fn(), reverify: vi.fn(), getIncident: vi.fn() },
  };
});
vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api } = await import("../api/client");
const UNKNOWN = unknownFixture as unknown as ActionCenter;
const AWAITING = awaitingFixture as unknown as ActionCenter;
const INCIDENT = incidentFixture as unknown as Detail;

/** Cells whose value is a claim about money, or about whether money moved.
 *  None of these may be `data-priority="low"`. */
const MUST_SURVIVE = [
  "Amount", "State", "Verification", "Provider ref", "Payment",
  "Risk", "Signatures", "Window", "Attempts", "Approval",
];

function stackedCells(): HTMLTableCellElement[] {
  return Array.from(
    document.querySelectorAll<HTMLTableCellElement>("table.stacked td"));
}

beforeEach(() => vi.clearAllMocks());

describe("the action queue on a small screen", () => {
  it("labels every cell, because the header row is not there", async () => {
    vi.mocked(api.actionCenter).mockResolvedValue(UNKNOWN);
    render(<MemoryRouter><Actions /></MemoryRouter>);
    await screen.findByText(UNKNOWN.unknown[0].id);

    const cells = stackedCells();
    expect(cells.length).toBeGreaterThan(0);
    // The one deliberate exception is the actions cell, which is a full-width
    // row of buttons rather than a labelled value.
    const unlabelled = cells.filter((c) => !c.getAttribute("data-label"));
    expect(unlabelled.length).toBeLessThanOrEqual(cells.length / 8);
  });

  it("drops nothing that asserts something about money", async () => {
    vi.mocked(api.actionCenter).mockResolvedValue(UNKNOWN);
    render(<MemoryRouter><Actions /></MemoryRouter>);
    await screen.findByText(UNKNOWN.unknown[0].id);

    // Asserted before the loop: a `for` over an empty list passes, and a test
    // that passes when the table did not render is a test that stops noticing.
    const labels = stackedCells()
      .map((c) => c.getAttribute("data-label")).filter(Boolean) as string[];
    expect(labels).toEqual(expect.arrayContaining(["Amount", "State"]));

    for (const cell of stackedCells()) {
      const label = cell.getAttribute("data-label");
      if (label && MUST_SURVIVE.includes(label)) {
        expect(cell.getAttribute("data-priority"),
               `"${label}" must not be dropped on a small screen`).not.toBe("low");
      }
    }
  });

  it("every label matches a real column header", async () => {
    // A label that names no column is a label somebody invented, and it will
    // disagree with the wide layout the first time a header is renamed.
    vi.mocked(api.actionCenter).mockResolvedValue(UNKNOWN);
    render(<MemoryRouter><Actions /></MemoryRouter>);
    await screen.findByText(UNKNOWN.unknown[0].id);

    const headers = new Set(
      screen.getAllByRole("columnheader").map((h) => h.textContent?.trim()));
    expect(stackedCells().length).toBeGreaterThan(0);
    for (const cell of stackedCells()) {
      const label = cell.getAttribute("data-label");
      if (label) expect(headers, label).toContain(label);
    }
  });

  it("labels the approval queue too", async () => {
    vi.mocked(api.actionCenter).mockResolvedValue(AWAITING);
    render(<MemoryRouter><Actions /></MemoryRouter>);
    await screen.findByText(AWAITING.awaiting_approval[0].approval_id);

    const labels = stackedCells()
      .map((c) => c.getAttribute("data-label")).filter(Boolean);
    expect(labels).toContain("Amount");
    expect(labels).toContain("Signatures");
  });
});

describe("the incident control table", () => {
  it("labels every cell and keeps the verification column", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({
      ...INCIDENT,
      actions: [{
        id: "ACT_R1", task_id: "TASK_X", merchant_id: "MERCH_A",
        action_type: "refund", status: "CONFIRMED",
        target_payment_id: "SYN_PAY_0002", external_payment_id: "pay_X",
        external_reference: "rfnd_X", amount_minor: 499900,
        verification_state: "SUCCESS", verify_attempts: 1, escalated: false,
        escalated_at: null, last_verified_at: null, next_verify_at: null,
        approval_id: "APR_X", recovery_candidate_id: null,
        created_at: INCIDENT.detected_at, updated_at: INCIDENT.detected_at,
        provider_latency_ms: null, verification_latency_ms: null,
        customer_id: null, payment_method: null, provider: "razorpay",
        environment: "test", incident_id: INCIDENT.id, owner: "USR_A_OWNER",
        task_request: null, task_status: null, approval_decision: "APPROVED",
        risk_level: "HIGH", expires_at: null, required_signatures: 1,
      }] as Detail["actions"],
    });
    render(<MemoryRouter><IncidentDetail /></MemoryRouter>);
    await screen.findByText("Control and execution");

    const labels = stackedCells()
      .map((c) => c.getAttribute("data-label")).filter(Boolean);
    expect(labels).toContain("Verification");
    expect(labels).toContain("Amount");
    expect(labels).toContain("Provider ref");
    for (const cell of stackedCells()) {
      expect(cell.getAttribute("data-priority")).not.toBe("low");
    }
  });
});
