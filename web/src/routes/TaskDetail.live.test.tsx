// Renders the page against a payload captured verbatim from a running API
// (src/test-fixtures/*.json), rather than one written from the TypeScript
// types. That distinction is not academic: the types once said
// `verification_detail: string`, the hand-written fixture agreed, and the page
// crashed the moment it met the real dict.
//
// Refresh with:
//   curl -s localhost:8000/tasks/<id>       -H "Authorization: Bearer <tok>"
//   curl -s localhost:8000/tasks/<id>/trace -H "Authorization: Bearer <tok>"

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task, TraceEvent } from "../api/types";
import TaskDetail from "./TaskDetail";
import taskFixture from "../test-fixtures/task.json";
import traceFixture from "../test-fixtures/trace.json";
import evidenceFixture from "../test-fixtures/evidence.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { getTask: vi.fn(), getTrace: vi.fn(), getEvidence: vi.fn(), approve: vi.fn(),
           reject: vi.fn(), reverify: vi.fn(), replay: vi.fn() },
  };
});

const { api } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const TASK = taskFixture as unknown as Task;
const TRACE = (traceFixture as { trace: TraceEvent[] }).trace;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.getTask.mockResolvedValue(TASK);
  mocked.getTrace.mockResolvedValue({ task_id: TASK.id, trace: TRACE });
  mocked.getEvidence.mockResolvedValue(evidenceFixture);
  render(
    <MemoryRouter initialEntries={[`/tasks/${TASK.id}`]}>
      <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>
    </MemoryRouter>,
  );
});

describe("a real completed task", () => {
  it("renders without throwing on any live field", async () => {
    expect(await screen.findByText("COMPLETED")).toBeInTheDocument();
    // The request appears twice on purpose — in the header, and again as the
    // summary of the task_created trace event — so scope rather than assume.
    expect(document.querySelector(".page-head .request")).toHaveTextContent(TASK.request);
    // The failure mode this whole file exists for.
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("shows the verification verdict with the sentence behind it", async () => {
    await screen.findByText("COMPLETED");
    const action = document.querySelector<HTMLElement>(".action-card")!;
    expect(within(action).getByText("SUCCESS")).toBeInTheDocument();
    expect(within(action).getByText(/amount_refunded increased by 499900 minor units/))
      .toBeInTheDocument();
    expect(within(action).getByText("expected vs actual")).toBeInTheDocument();
  });

  it("keeps the decided approval on the record", async () => {
    // A page that forgets who approved a refund the moment it executes is
    // missing the part an auditor came for.
    expect(await screen.findByText("Approval history")).toBeInTheDocument();
    expect(screen.getByText("APPROVED")).toBeInTheDocument();
  });

  it("summarises each trace event instead of only naming it", async () => {
    expect(await screen.findByText("Audit trace")).toBeInTheDocument();
    expect(screen.getByText(/ALLOW · find_duplicate_payments · low_risk_authorized/))
      .toBeInTheDocument();
    expect(screen.getByText(/CONFIRMED · rfnd_MOCK/)).toBeInTheDocument();
  });

  it("filters the trace by stage", async () => {
    const trace = (await screen.findByText("Audit trace")).closest<HTMLElement>(".card")!;
    expect(within(trace).getByText(`${TRACE.length} of ${TRACE.length}`)).toBeInTheDocument();

    await userEvent.click(within(trace).getByRole("button", { name: "Verification" }));
    expect(within(trace).getByText(`1 of ${TRACE.length}`)).toBeInTheDocument();
    expect(within(trace).getByText("verification")).toBeInTheDocument();
    expect(within(trace).queryByText("llm_turn")).toBeNull();
  });

  it("offers no approval gate and no re-verify on a settled task", async () => {
    await screen.findByText("COMPLETED");
    expect(screen.queryByRole("button", { name: /Approve and execute/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Re-verify/ })).toBeNull();
  });
});
