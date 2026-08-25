// The client is where a frontend bug could misrepresent a financial state:
// swallowing a 409 the approval state machine raised, or sending a request
// without the identity the server authorises against. Those are the tests.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, getToken, setToken } from "./client";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Assert the call rejects, and hand back the error typed.
 *  `.catch(e => e as ApiError)` types the result as a union with the success
 *  value, which silently weakens every assertion that follows — and if the call
 *  ever stops rejecting, the failure should say so rather than complain about a
 *  missing property. */
async function rejection(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the request to reject, but it resolved");
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as Response;
}

describe("token storage", () => {
  it("round-trips a token and clears it on empty", () => {
    setToken("abc.def");
    expect(getToken()).toBe("abc.def");
    setToken("");
    expect(getToken()).toBe("");
  });

  it("treats an unreadable store as empty rather than throwing", () => {
    // Private browsing and some embedded webviews throw on access. The app must
    // render a sign-in screen, not a blank page.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(getToken()).toBe("");
  });
});

describe("authentication", () => {
  it("refuses to send an authenticated request without a token", async () => {
    await expect(api.getTask("TASK_1")).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the bearer token on authenticated routes", async () => {
    setToken("USR_A_OWNER.sig");
    fetchMock.mockResolvedValue(jsonResponse({ id: "TASK_1" }));
    await api.getTask("TASK_1");
    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer USR_A_OWNER.sig");
  });

  it("does not require a token for /health", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await api.health();
    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });

  it("percent-encodes the task id rather than interpolating it raw", async () => {
    setToken("t");
    fetchMock.mockResolvedValue(jsonResponse({}));
    await api.getTask("TASK/../health");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tasks/TASK%2F..%2Fhealth");
  });
});

describe("error normalisation", () => {
  it("surfaces the code from a 409 the approval state machine raised", async () => {
    setToken("t");
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: { error: "Approval has expired.", code: "APPROVAL_EXPIRED" } }, 409),
    );
    const err = await rejection(api.approve("TASK_1"));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.code).toBe("APPROVAL_EXPIRED");
    expect(err.message).toBe("Approval has expired.");
    expect(err.isConflict).toBe(true);
    expect(err.isAuth).toBe(false);
  });

  it("handles a plain-string detail (FastAPI's 404 shape)", async () => {
    setToken("t");
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Unknown task." }, 404));
    const err = await rejection(api.getTask("nope"));
    expect(err.status).toBe(404);
    expect(err.message).toBe("Unknown task.");
    expect(err.code).toBeUndefined();
  });

  it("flags a 401 as an auth failure so the UI can ask for a token", async () => {
    setToken("stale");
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Invalid token." }, 401));
    const err = await rejection(api.getTask("TASK_1"));
    expect(err.isAuth).toBe(true);
  });

  it("says the API is unreachable rather than 'Failed to fetch'", async () => {
    setToken("t");
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await rejection(api.getTask("TASK_1"));
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/Cannot reach the API/);
  });

  it("does not throw on a non-JSON error body", async () => {
    setToken("t");
    fetchMock.mockResolvedValue({
      ok: false, status: 502, statusText: "Bad Gateway",
      text: async () => "<html>proxy error</html>",
    } as Response);
    const err = await rejection(api.getTask("TASK_1"));
    expect(err.status).toBe(502);
    expect(err.message).toBe("<html>proxy error</html>");
  });
});

describe("replay", () => {
  it("asks for the mode the caller named", async () => {
    setToken("t");
    fetchMock.mockResolvedValue(jsonResponse({ external_calls: 0 }));
    await api.replay("TASK_1", "RE_REASON");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tasks/TASK_1/replay?mode=RE_REASON");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });
});
