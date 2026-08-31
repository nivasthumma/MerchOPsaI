// MerchantOps §37, §56, §66 on the task page. The API carried all three and
// the page showed none of them.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../api/types";
import TaskDetail from "./TaskDetail";
import taskFixture from "../test-fixtures/task.json";
import traceFixture from "../test-fixtures/trace.json";
import evidenceFixture from "../test-fixtures/evidence.json";
import messagesFixture from "../test-fixtures/messages.json";

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useParams: () => ({ taskId: "TASK_ABC" }) };
});
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { getTask: vi.fn(), getTrace: vi.fn(), getEvidence: vi.fn(),
           getMessages: vi.fn(), approve: vi.fn(), reject: vi.fn(),
           reverify: vi.fn(), replay: vi.fn() },
  };
});

const { api } = await import("../api/client");
const base = taskFixture as unknown as Task;

function mount(task: Partial<Task> = {}) {
  vi.mocked(api.getTask).mockResolvedValue({ ...base, ...task } as Task);
  vi.mocked(api.getTrace).mockResolvedValue(traceFixture as never);
  vi.mocked(api.getEvidence).mockResolvedValue(evidenceFixture as never);
  vi.mocked(api.getMessages).mockResolvedValue(messagesFixture as never);
  render(<MemoryRouter><TaskDetail /></MemoryRouter>);
}

describe("what the agent concluded (§37)", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("shows the intent and recommendation the task carried", async () => {
    mount();
    expect(await screen.findByText("What the agent concluded")).toBeInTheDocument();
    expect(screen.getByText("duplicate_payment")).toBeInTheDocument();
    expect(screen.getByText("refund_duplicate")).toBeInTheDocument();
  });

  it("labels confidence as the model's own number, not a quality score", async () => {
    mount({ agent_confidence: 0.9 });
    const v = await screen.findByText("0.90");
    expect(v).toHaveAttribute("title", expect.stringMatching(/consulted by nothing/i));
  });

  it("says a human is required when policy says so and the model did not", async () => {
    /* The model may raise the bar and never lower it, so a model "no" beside a
       pending approval must not read as a disagreement it can win. */
    mount({ requires_human: true, model_requires_human: false });
    expect(await screen.findByText(/Policy requires a human here/)).toBeInTheDocument();
  });

  it("shows no conclusion card when the task carries none", async () => {
    mount({ intent: null, recommendation: null, agent_confidence: null });
    await screen.findByRole("tablist");
    expect(screen.queryByText("What the agent concluded")).not.toBeInTheDocument();
  });
});

describe("what a failure means (§56)", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("tells an operator whether to retry, not just what broke", async () => {
    mount({
      status: "COMPLETED", failure_code: "EXTERNAL_STATE_UNKNOWN",
      failure: {
        error_code: "EXTERNAL_STATE_UNKNOWN", category: "UNKNOWN_EXTERNAL_STATE",
        retryability: "RECONCILE", owning_subsystem: "reconciliation_engine",
        recommended_next_action: "Read provider state. Never re-issue the action.",
        correlation_id: null, is_classified: true,
      },
    });
    expect(await screen.findByText("UNKNOWN_EXTERNAL_STATE")).toBeInTheDocument();
    expect(screen.getByText(/Read provider state\. Never re-issue the action\./))
      .toBeInTheDocument();
    expect(screen.getByText("reconciliation_engine")).toBeInTheDocument();
  });

  it("flags a code the taxonomy does not know", async () => {
    mount({
      failure: {
        error_code: "SOMETHING_NEW", category: "INTERNAL_ERROR",
        retryability: "ESCALATE", owning_subsystem: "platform",
        recommended_next_action: "An unclassified failure.",
        correlation_id: null, is_classified: false,
      },
    });
    expect(await screen.findByText(/gap in the taxonomy, not a\s+transient condition/))
      .toBeInTheDocument();
  });

  it("shows nothing when nothing failed", async () => {
    mount({ failure: null });
    await screen.findByRole("tablist");
    expect(screen.queryByText("Failure")).not.toBeInTheDocument();
  });
});

describe("what the model saw (§66)", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("renders the conversation, marking the quarantined parts", async () => {
    mount();
    await screen.findByRole("tablist");
    await userEvent.click(screen.getByRole("tab", { name: /transcript/i }));

    const list = await screen.findByRole("list", { name: "Transcript" });
    const items = within(list).getAllByRole("listitem");
    expect(items.length).toBe(messagesFixture.messages.length);

    // The fixture is a live response and carries one untrusted message.
    const flagged = items.filter((li) => li.hasAttribute("data-untrusted"));
    expect(flagged.length).toBe(
      messagesFixture.messages.filter((m) => m.contains_untrusted).length);
    expect(within(flagged[0]).getByText("untrusted")).toBeInTheDocument();
  });

  it("is a different tab from the trace, because they answer different questions",
     async () => {
    mount();
    await screen.findByRole("tablist");
    expect(screen.getByRole("tab", { name: /^Trace/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /transcript/i })).toBeInTheDocument();
  });
});
