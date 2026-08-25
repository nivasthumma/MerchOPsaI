// The provider control moved out of the shell and into Settings: a warning
// belongs in front of you, a control belongs somewhere you went on purpose.
// The guarantees it has to keep did not move with it — they are all still here.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import Settings from "./Settings";
import type { Health } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { health: vi.fn(), me: vi.fn(), setProvider: vi.fn(),
                           metrics: vi.fn() } };
});

const { api } = await import("../api/client");
const health = api.health as unknown as ReturnType<typeof vi.fn>;
const me = api.me as unknown as ReturnType<typeof vi.fn>;
const setProvider = api.setProvider as unknown as ReturnType<typeof vi.fn>;
const metrics = api.metrics as unknown as ReturnType<typeof vi.fn>;

const OWNER = {
  user_id: "USR_A_OWNER", merchant_id: "MERCH_A", role: "owner",
  permissions: ["read:metrics", "read:orders", "action:refund"],
};
const ANALYST = { ...OWNER, user_id: "USR_A_ANALYST", role: "analyst",
                  permissions: ["read:metrics", "read:orders"] };

const OK: Health = {
  status: "ok", llm_provider: "deterministic", llm_credential_source: null,
  llm_provider_is_explicit: false, llm_provider_source: "auto",
  llm_model: "deterministic-planner-v1",
  payment_adapter: "mock", razorpay_execution_is_real: false,
  auth: "bearer_hmac", auth_secret_is_development_default: false,
};

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={["/settings"]}>
      <Routes>
        <Route path="/" element={<App />}>
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  me.mockResolvedValue(OWNER);
  metrics.mockResolvedValue({
    window_hours: 24, gated: 1, approved: 2, rejected: 0, moved_minor: 499900,
    tool_calls: 9, tool_errors: 0, tool_error_rate: 0, p50_duration_ms: 21,
    signing_secret_is_development_default: false,
  });
  localStorage.setItem("merchantops.token", "t");
});

describe("reasoning provider control", () => {
  it("offers the switch to an owner", async () => {
    health.mockResolvedValue(OK);
    renderSettings();
    expect(await screen.findByText("Reasoning provider")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "anthropic" })).toBeEnabled();
  });

  it("never offers a field for a credential", async () => {
    // CONTRACT §37 keeps provider secrets in the environment. A browser form is
    // neither an environment variable nor an appropriate secret mechanism.
    health.mockResolvedValue(OK);
    renderSettings();
    await screen.findByText("Reasoning provider");
    expect(screen.getByText(/there is no field for a key here/)).toBeInTheDocument();
    const inputs = screen.queryAllByRole("textbox");
    expect(inputs.every((i) => !/key|secret|token/i.test(i.getAttribute("aria-label") ?? "")))
      .toBe(true);
  });

  it("tells an analyst it is not theirs to change", async () => {
    health.mockResolvedValue(OK);
    me.mockResolvedValue(ANALYST);
    renderSettings();
    expect(await screen.findByText(/Changing it requires the owner role/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "anthropic" })).toBeNull();
  });

  it("surfaces the server's refusal when no credential is configured", async () => {
    health.mockResolvedValue(OK);
    setProvider.mockRejectedValue(
      new Error("No Anthropic credential is configured on the server."));
    renderSettings();
    await userEvent.click(await screen.findByRole("button", { name: "anthropic" }));
    expect(await screen.findByText(/No Anthropic credential is configured/))
      .toBeInTheDocument();
  });

  it("warns that published metrics were not measured on a model", async () => {
    health.mockResolvedValue({ ...OK, llm_provider: "anthropic",
                               llm_provider_source: "runtime",
                               llm_model: "claude-opus-5",
                               llm_credential_source: "api_key" });
    renderSettings();
    expect(await screen.findByText(/Published metrics were measured on the deterministic planner/))
      .toBeInTheDocument();
  });
});

describe("resolved configuration", () => {
  it("reports what the server said, not what the page assumed", async () => {
    health.mockResolvedValue({ ...OK, auth_secret_is_development_default: true });
    renderSettings();
    expect(await screen.findByText("Resolved configuration")).toBeInTheDocument();
    expect(screen.getAllByText(/development default/).length).toBeGreaterThan(0);
  });

  it("refuses to show settings it could not read", async () => {
    health.mockRejectedValue(new Error("down"));
    renderSettings();
    // No stale or invented values: the page says why it is empty instead.
    expect(await screen.findByText(/nothing trustworthy to show/)).toBeInTheDocument();
    expect(screen.queryByText("Resolved configuration")).toBeNull();
  });
});
