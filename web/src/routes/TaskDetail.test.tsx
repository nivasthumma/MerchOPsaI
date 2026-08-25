// The approval screen is the one place in this app where a rendering decision
// could cause money to move, or appear to have moved. These tests pin the
// behaviours ADR-0015 claims: the button is never gated client-side, a refusal
// is shown with its reason, and an unsettled action is shown as unsettled.

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ToastHost } from "../components/Toast";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { Task } from "../api/types";
import TaskDetail from "./TaskDetail";
import playbackFixture from "../test-fixtures/playback.json";
import rereasonFixture from "../test-fixtures/rereason.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      getTask: vi.fn(), getTrace: vi.fn(), getEvidence: vi.fn(), approve: vi.fn(),
      reject: vi.fn(), reverify: vi.fn(), replay: vi.fn(),
    },
  };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const BASE: Task = {
  id: "TASK_ABC", merchant_id: "MERCH_A", user_id: "USR_A_OWNER",
  request: "Find the duplicate payment and refund it",
  status: "AWAITING_APPROVAL", final_answer: null, failure_code: null,
  findings: [], tool_calls: 3, llm_turns: 3, duration_ms: 40,
  agent_version: "merchantops-agent/0.1.0", model_version: "deterministic-planner-v1",
  prompt_version: "investigator-v1", is_replay: false, replayed_from: null,
  approvals: [{
    id: "APR_1", decision: "PENDING", action_type: "request_refund",
    action_payload: { synthetic_payment_id: "SYN_PAY_0002", amount_minor: 499900,
                      reason: "Duplicate payment: a second capture was recorded." },
    risk_level: "HIGH", expires_at: new Date(Date.now() + 9e5).toISOString(),
    decided_by: null,
  }],
  actions: [],
};

/** MemoryRouter keeps history in memory and never touches window.location, so
 *  a URL assertion has to read the router's own location. */
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.search}</div>;
}

function renderAt(task: Task, trace: unknown[] = []) {
  mocked.getTask.mockResolvedValue(task);
  mocked.getTrace.mockResolvedValue({ task_id: task.id, trace });
  mocked.getEvidence.mockResolvedValue({ task_id: task.id, tool_calls: [] });
  return render(
    <ToastHost>
      <MemoryRouter initialEntries={[`/tasks/${task.id}`]}>
        <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>
        <LocationProbe />
      </MemoryRouter>
    </ToastHost>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe("pending approval", () => {
  it("states that no external call has been made", async () => {
    renderAt(BASE);
    expect(await screen.findByText(/Approval required/)).toBeInTheDocument();
    expect(screen.getByText(/No external call has been made/)).toBeInTheDocument();
  });

  it("shows the amount in rupees with the minor units preserved", async () => {
    renderAt(BASE);
    expect(await screen.findByTitle("499900 minor units")).toHaveTextContent("₹4,999.00");
  });

  it("leaves the approve button enabled — authorization is the server's call", async () => {
    renderAt(BASE);
    const btn = await screen.findByRole("button", { name: /Approve and execute/ });
    expect(btn).toBeEnabled();
  });

  it("sends the approval and reloads the task", async () => {
    renderAt(BASE);
    mocked.approve.mockResolvedValue({ ...BASE, status: "COMPLETED" });
    await userEvent.click(await screen.findByRole("button", { name: /Approve and execute/ }));
    await waitFor(() => expect(mocked.approve).toHaveBeenCalledWith("TASK_ABC"));
    // Reloaded rather than trusting the response we already have.
    await waitFor(() => expect(mocked.getTask).toHaveBeenCalledTimes(2));
  });

  it("shows a server refusal with its code instead of failing silently", async () => {
    renderAt(BASE);
    mocked.approve.mockRejectedValue(
      new ApiError(409, "Approval has expired.", "APPROVAL_EXPIRED"));
    await userEvent.click(await screen.findByRole("button", { name: /Approve and execute/ }));
    expect(await screen.findByText("APPROVAL_EXPIRED")).toBeInTheDocument();
    expect(screen.getAllByText(/Approval has expired/).length).toBeGreaterThan(0);
  });

  it("announces the refusal as well as writing it into the page", async () => {
    // Announced *and* written. The toast is a courtesy; the banner is the record.
    renderAt(BASE);
    mocked.approve.mockRejectedValue(
      new ApiError(409, "Approval has expired.", "APPROVAL_EXPIRED"));
    await userEvent.click(await screen.findByRole("button", { name: /Approve and execute/ }));
    expect(await screen.findByText("Refused by the server")).toBeInTheDocument();
    expect(document.querySelector(".banner.warn")).toBeInTheDocument();
  });

  it("announces an approval with the verification state it produced", async () => {
    renderAt(BASE);
    mocked.approve.mockResolvedValue({
      ...BASE, status: "COMPLETED", approvals: [],
      actions: [{ id: "ACT_1", action_type: "request_refund", status: "CONFIRMED",
                  target_payment_id: "SYN_PAY_0002", external_payment_id: "pay_test_002",
                  amount_minor: 499900, external_reference: "rfnd_MOCK1",
                  verification_state: "SUCCESS", verification_detail: null,
                  verify_attempts: 1 }],
    });
    await userEvent.click(await screen.findByRole("button", { name: /Approve and execute/ }));
    expect(await screen.findByText("Approved and executed")).toBeInTheDocument();
    expect(screen.getByText(/Independent verification: SUCCESS/)).toBeInTheDocument();
  });
});

describe("unsettled actions", () => {
  const unknown: Task = {
    ...BASE, status: "COMPLETED", approvals: [],
    actions: [{
      id: "ACT_1", action_type: "request_refund", status: "SUBMITTED",
      target_payment_id: "SYN_PAY_0002", external_payment_id: "pay_test_002",
      amount_minor: 499900, external_reference: null,
      verification_state: "UNKNOWN",
      // The real shape: agent_actions.verification_detail is a JSON column, not
      // a string. An earlier fixture here said "string" because that is what the
      // type claimed, so the test agreed with the bug instead of catching it.
      verification_detail: {
        state: "UNKNOWN",
        reason: "Connection lost after the request was submitted.",
        expected: { external_payment_id: "pay_test_002", refund_amount_minor: 499900 },
        actual: { amount_refunded_minor: 499900, refund_status: "full" },
        external_reference: null,
      },
      verify_attempts: 1,
    }],
  };

  it("renders UNKNOWN and offers re-verification", async () => {
    renderAt(unknown);
    expect(await screen.findByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText(/1 action unsettled/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Re-verify/ })).toBeEnabled();
  });

  it("says re-verification never re-issues the action", async () => {
    renderAt(unknown);
    expect(await screen.findByText(/never\s+re-issues the action/)).toBeInTheDocument();
  });

  it("renders the verification reason, not the object it lives in", async () => {
    // Regression: `verification_detail` is a dict, and rendering it directly
    // threw "Objects are not valid as a React child" — a whole-page crash on
    // the one screen that reports whether money moved.
    renderAt(unknown);
    expect(await screen.findByText(/Connection lost after the request was submitted/))
      .toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("keeps the expected-vs-actual evidence available", async () => {
    renderAt(unknown);
    expect(await screen.findByText("expected vs actual")).toBeInTheDocument();
  });

  it("offers no re-verify button once everything is settled", async () => {
    renderAt({ ...unknown, actions: [{ ...unknown.actions[0],
      verification_state: "SUCCESS", external_reference: "rfnd_MOCK1",
      verification_detail: {
        state: "SUCCESS",
        reason: "Confirmed: amount_refunded increased by 499900 minor units.",
        external_reference: "rfnd_MOCK1",
      } }] });
    expect(await screen.findByText("SUCCESS")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-verify/ })).toBeNull();
  });
});

describe("replay", () => {
  // Regression: the count lives in `external_calls_made`. Reading
  // `external_calls` yielded undefined, failed the `=== 0` check, and reported
  // a clean replay as "a defect — replay must never move money".
  it("reads the count from the field the API actually sends", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] });
    mocked.replay.mockResolvedValue(rereasonFixture);
    await userEvent.click(await screen.findByRole("button", { name: "RE_REASON" }));
    expect(await screen.findByText(/RE_REASON: 0 external calls/)).toBeInTheDocument();
    expect(screen.getByText(/No financial side effect/)).toBeInTheDocument();
    expect(screen.queryByText(/This is a defect/)).toBeNull();
  });

  it("still calls a real external call a defect", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] });
    mocked.replay.mockResolvedValue({ ...rereasonFixture, external_calls_made: 1 });
    await userEvent.click(await screen.findByRole("button", { name: "RE_REASON" }));
    expect(await screen.findByText(/This is a defect/)).toBeInTheDocument();
  });

  it("shows the divergence verdicts and the sequence comparison", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] });
    mocked.replay.mockResolvedValue(rereasonFixture);
    await userEvent.click(await screen.findByRole("button", { name: "RE_REASON" }));
    expect(await screen.findByText("reasoning identical")).toBeInTheDocument();
    expect(screen.getByText("policy identical")).toBeInTheDocument();
    expect(screen.getByText("original actions unchanged")).toBeInTheDocument();
    expect(screen.getByText(/same tools in the same order/)).toBeInTheDocument();
  });

  it("shows each replayed step with the policy decision it got", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] });
    mocked.replay.mockResolvedValue(playbackFixture);
    await userEvent.click(await screen.findByRole("button", { name: "PLAYBACK" }));
    const row = (await screen.findByText("get_revenue_summary")).closest<HTMLElement>("tr")!;
    expect(within(row).getByText("ALLOW")).toBeInTheDocument();
    expect(within(row).getByText("LOW")).toBeInTheDocument();
  });
});

describe("live updates", () => {
  it("polls while a task still waits on a human, and stops when it cannot change", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt(BASE);                       // AWAITING_APPROVAL
    await screen.findByText("Approval required");
    expect(await screen.findByText("live")).toBeInTheDocument();

    const before = mocked.getTask.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(11000); });
    expect(mocked.getTask.mock.calls.length).toBeGreaterThan(before);
    vi.useRealTimers();
  });

  it("does not poll a task that has finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt({ ...BASE, status: "COMPLETED", approvals: [], actions: [] });
    await screen.findByText("COMPLETED");
    expect(screen.queryByText("live")).toBeNull();

    const before = mocked.getTask.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(20000); });
    expect(mocked.getTask.mock.calls.length).toBe(before);
    vi.useRealTimers();
  });
});

describe("trace controls", () => {
  it("puts the stage filter in the URL so it can be linked", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] }, [
      { id: 1, at: new Date().toISOString(), event: "policy_decision",
        payload: { decision: "ALLOW", tool: "get_order", rule: "low_risk_authorized" } },
      { id: 2, at: new Date().toISOString(), event: "verification",
        payload: { state: "SUCCESS" } },
    ]);
    await screen.findByText("Audit trace");
    await userEvent.click(screen.getByRole("button", { name: "Verification" }));
    expect(screen.getByTestId("location")).toHaveTextContent("stage=verification");
  });

  it("searches summaries and payloads, not only event names", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] }, [
      { id: 1, at: new Date().toISOString(), event: "policy_decision",
        payload: { decision: "DENY", tool: "request_refund", rule: "missing_permission" } },
      { id: 2, at: new Date().toISOString(), event: "llm_turn",
        payload: { turn: 1, stop_reason: "tool_use", requested_tools: [] } },
    ]);
    await screen.findByText("Audit trace");
    await userEvent.type(screen.getByLabelText("Search the trace"), "missing_permission");
    expect(screen.getByText("policy_decision")).toBeInTheDocument();
    expect(screen.queryByText("llm_turn")).toBeNull();
  });
});
