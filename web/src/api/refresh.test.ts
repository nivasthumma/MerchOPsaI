import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { api, setToken, setRefreshToken, getRefreshToken } from "./client";

/** Refreshing an expired access token — ADR-0049.
 *
 *  The property that matters is that ONE 401 causes ONE refresh and ONE retry.
 *  A loop around an endpoint that mints credentials turns an expired session
 *  into a flood, and a refresh token reused because the client kept the old one
 *  signs the account out of everything. */

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setToken("mo1.old");
  setRefreshToken("mo1.refresh");
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setToken("");
  setRefreshToken("");
});

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  }));

describe("refreshing an expired token", () => {
  it("refreshes once and retries the request", async () => {
    fetchMock
      .mockImplementationOnce(() => json({ detail: "expired" }, 401))
      .mockImplementationOnce(() => json({ access_token: "mo1.new",
                                           refresh_token: "mo1.refresh2" }))
      .mockImplementationOnce(() => json({ user_id: "USR_A_OWNER" }));

    const me = await api.me();
    expect(me.user_id).toBe("USR_A_OWNER");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // The new refresh token replaced the old one. Keeping the old would make
    // the next attempt a replay, which signs the account out of everything.
    expect(getRefreshToken()).toBe("mo1.refresh2");
  });

  it("does not retry more than once", async () => {
    fetchMock
      .mockImplementationOnce(() => json({ detail: "expired" }, 401))
      .mockImplementationOnce(() => json({ access_token: "mo1.new",
                                           refresh_token: "mo1.refresh2" }))
      .mockImplementationOnce(() => json({ detail: "still no" }, 401));

    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("gives up and forgets a refresh token the server rejects", async () => {
    fetchMock
      .mockImplementationOnce(() => json({ detail: "expired" }, 401))
      .mockImplementationOnce(() => json({ detail: "replayed" }, 401));

    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(getRefreshToken()).toBe("");
  });

  it("does not try to refresh when there is nothing to refresh with", async () => {
    setRefreshToken("");
    fetchMock.mockImplementationOnce(() => json({ detail: "no" }, 401));

    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
