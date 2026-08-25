// The approval screen is the one place in this app where a rendering decision
// could cause money to move, or appear to have moved. These tests pin the
// behaviours ADR-0015 claims: the button is never gated client-side, a refusal
// is shown with its reason, and an unsettled action is shown as unsettled.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { Task } from "../api/types";
import TaskDetail from "./TaskDetail";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      getTask: vi.fn(), getTrace: vi.fn(), approve: vi.fn(),
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

function renderAt(task: Task, trace: unknown[] = []) {
  mocked.getTask.mockResolvedValue(task);
  mocked.getTrace.mockResolvedValue({ task_id: task.id, trace });
  return render(
    <MemoryRouter initialEntries={[`/tasks/${task.id}`]}>
      <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>
    </MemoryRouter>,
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
    expect(screen.getByText(/Approval has expired/)).toBeInTheDocument();
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
      verification_detail: "Connection lost after the request was submitted.",
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

  it("offers no re-verify button once everything is settled", async () => {
    renderAt({ ...unknown, actions: [{ ...unknown.actions[0],
      verification_state: "SUCCESS", external_reference: "rfnd_MOCK1" }] });
    expect(await screen.findByText("SUCCESS")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-verify/ })).toBeNull();
  });
});

describe("replay", () => {
  it("calls a zero-external-call replay correct, and a non-zero one a defect", async () => {
    renderAt({ ...BASE, status: "COMPLETED", approvals: [] });
    mocked.replay.mockResolvedValue({ external_calls: 0, steps: ["get_order"] });
    await userEvent.click(await screen.findByRole("button", { name: "PLAYBACK" }));
    expect(await screen.findByText(/No financial side effect/)).toBeInTheDocument();

    mocked.replay.mockResolvedValue({ external_calls: 1 });
    await userEvent.click(screen.getByRole("button", { name: "RE_REASON" }));
    expect(await screen.findByText(/This is a defect/)).toBeInTheDocument();
  });
});
