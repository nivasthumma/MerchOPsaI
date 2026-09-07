// MerchantOps §51 and plan P0-07. The fixture is a live /incidents/{id}
// response, with one untrusted evidence row appended so the quarantine
// rendering is exercised.
//
// The section names changed when the page was reordered into the decision
// sequence (WHAT HAPPENED → ... → VERIFICATION). Every assertion below is the
// one that was here before, re-pointed at the section that now carries it —
// nothing was dropped because a heading moved.

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
  await screen.findByText("Why we believe it");
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
    expect(screen.getByText("Expected recovery")).toBeInTheDocument();
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

describe("the decision sequence — P0-07", () => {
  beforeEach(() => {
    vi.mocked(api.getIncident).mockReset();
    vi.mocked(api.getIncident).mockResolvedValue(data);
  });

  it("orders the page as the decision is made, not as the data model is shaped", async () => {
    await renderPage();
    const headings = screen.getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings).toEqual([
      "What happened",
      "Why we believe it",
      "Business impact",
      "Recovery recommendation",
      "Control and execution",
      "Investigations",
      "Timeline",
    ]);
  });

  it("counts independent sources rather than only listing evidence", async () => {
    // "Success rate dropped" from one signal and the same claim corroborated by
    // four are different claims.
    await renderPage();
    expect(screen.getByText(/independent source/)).toBeInTheDocument();
  });

  it("says a single source corroborates nothing", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({
      ...data,
      evidence: [data.evidence[0]],
    });
    await renderPage();
    expect(screen.getByText(/corroborates nothing on its own/)).toBeInTheDocument();
  });

  it("shows the §12 detection metadata from the canonical keys", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({
      ...data,
      signals: { ...data.signals, baseline: 91.8, observed: 73.2,
                 threshold: "drop >= 15pp", unit: "%" },
    });
    await renderPage();
    expect(screen.getByText("Baseline")).toBeInTheDocument();
    expect(screen.getByText("91.8%")).toBeInTheDocument();
    expect(screen.getByText("73.2%")).toBeInTheDocument();
    expect(screen.getByText("drop >= 15pp")).toBeInTheDocument();
  });

  it("says so when a rule publishes no baseline, rather than showing a zero", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({ ...data, signals: {} });
    await renderPage();
    expect(screen.getByText("not published by this rule")).toBeInTheDocument();
    expect(screen.queryByText("Observed")).toBeNull();
  });

  it("does not claim an action happened when none has", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({ ...data, actions: [] });
    await renderPage();
    // A recommendation is not an action, and the page must not let the two
    // read alike.
    expect(screen.getByText(/A recommendation is not an action/))
      .toBeInTheDocument();
  });

  it("carries policy, approval, provider reference and verification for each action", async () => {
    vi.mocked(api.getIncident).mockResolvedValue({
      ...data,
      actions: [{
        id: "ACT_TEST01", task_id: "TASK_X", merchant_id: "MERCH_A",
        action_type: "refund", status: "UNKNOWN",
        target_payment_id: "SYN_PAY_0002", external_payment_id: "pay_X",
        external_reference: null, amount_minor: 499900,
        verification_state: "UNKNOWN", verify_attempts: 2, escalated: false,
        escalated_at: null, last_verified_at: null, next_verify_at: null,
        approval_id: "APR_TEST01", recovery_candidate_id: null,
        created_at: data.detected_at, updated_at: data.detected_at,
        provider_latency_ms: null, verification_latency_ms: null,
        customer_id: null, payment_method: null, provider: "razorpay",
        environment: "test", incident_id: "INC_TEST", owner: "USR_A_OWNER",
        task_request: null, task_status: null, approval_decision: "APPROVED",
        risk_level: "HIGH", expires_at: null, required_signatures: 1,
      }] as Detail["actions"],
    });
    await renderPage();

    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Action", "Amount", "Policy", "Approval",
                             "Provider reference", "Verification", "When"]);
    // No reference issued is itself the reason the outcome is unknown, and the
    // page says so rather than leaving a blank cell.
    expect(screen.getByTitle(/No reference was issued/)).toBeInTheDocument();
  });
});
