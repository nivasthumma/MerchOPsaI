import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { api, setToken } from "./client";
import type { Task } from "./types";

/** Polling a task that the server may or may not have finished (ADR-0045).
 *
 *  The client cannot know whether a submission ran inline or was queued, and
 *  should not have to — which is the property these tests pin. */

const BASE: Task = {
  id: "TASK_1",
  merchant_id: "MERCH_A",
  request: "why did revenue drop",
  status: "COMPLETED",
  findings: [],
  tool_call_count: 0,
  llm_turn_count: 0,
} as unknown as Task;

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setToken("USR_A_OWNER.sig");
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setToken("");
});

function reply(task: Task) {
  return Promise.resolve(new Response(JSON.stringify(task), {
    status: 200, headers: { "Content-Type": "application/json" },
  }));
}

describe("awaitTask", () => {
  it("returns a finished task without asking the server anything", async () => {
    const out = await api.awaitTask(BASE);
    expect(out.status).toBe("COMPLETED");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("polls a queued task until it stops moving", async () => {
    fetchMock
      .mockImplementationOnce(() => reply({ ...BASE, status: "RUNNING" }))
      .mockImplementationOnce(() => reply({ ...BASE, status: "COMPLETED" }));

    const out = await api.awaitTask({ ...BASE, status: "QUEUED" }, { intervalMs: 1 });
    expect(out.status).toBe("COMPLETED");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops on AWAITING_APPROVAL rather than waiting for a human", async () => {
    fetchMock.mockImplementationOnce(() =>
      reply({ ...BASE, status: "AWAITING_APPROVAL" }));

    const out = await api.awaitTask({ ...BASE, status: "QUEUED" }, { intervalMs: 1 });
    expect(out.status).toBe("AWAITING_APPROVAL");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("gives up rather than spinning forever on a queue nobody is draining", async () => {
    fetchMock.mockImplementation(() => reply({ ...BASE, status: "QUEUED" }));

    const out = await api.awaitTask({ ...BASE, status: "QUEUED" },
                                    { intervalMs: 1, timeoutMs: 15 });
    // Handed back in the state it actually reached, so the page can say what
    // happened instead of showing a spinner with no end.
    expect(out.status).toBe("QUEUED");
  });

  it("stops when the caller aborts", async () => {
    const control = new AbortController();
    fetchMock.mockImplementation(() => {
      control.abort();
      return reply({ ...BASE, status: "QUEUED" });
    });

    const out = await api.awaitTask({ ...BASE, status: "QUEUED" },
                                    { intervalMs: 1, signal: control.signal });
    expect(out.status).toBe("QUEUED");
  });
});
