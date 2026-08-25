// The shell's job is to state the run configuration before anyone acts on it.
// A demo that quietly executes against a mock, or an API signing tokens with a
// development secret, must be visible without being looked for.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Health } from "./api/types";

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return { ...actual, api: { health: vi.fn(), me: vi.fn(), setProvider: vi.fn(),
                           metrics: vi.fn() } };
});

const { api } = await import("./api/client");
const health = api.health as unknown as ReturnType<typeof vi.fn>;
const me = api.me as unknown as ReturnType<typeof vi.fn>;
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

function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<div>SIGNED IN</div>} />
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
});

describe("run configuration", () => {
  it("says refunds are mocked when the adapter is not live", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    expect(await screen.findByText(/execute against a mock adapter/)).toBeInTheDocument();
    // And is explicit that the safety machinery is identical either way.
    expect(screen.getByText(/Policy, approval, idempotency and\s+verification are identical/))
      .toBeInTheDocument();
  });

  it("says execution is real when it is", async () => {
    health.mockResolvedValue({ ...OK, razorpay_execution_is_real: true,
                               payment_adapter: "live_test_mode" });
    renderApp();
    expect(await screen.findByText(/Live Razorpay Test Mode/)).toBeInTheDocument();
  });

  it("distinguishes 'no credential found' from a deliberate choice", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    expect(await screen.findByText(/no Anthropic credential detected/)).toBeInTheDocument();
  });

  it("warns loudly about a development signing secret", async () => {
    health.mockResolvedValue({ ...OK, auth_secret_is_development_default: true });
    renderApp();
    expect(await screen.findByText(/Development signing secret in use/)).toBeInTheDocument();
  });

  it("says the API is unreachable rather than rendering an empty shell", async () => {
    health.mockRejectedValue(new Error("down"));
    renderApp();
    expect(await screen.findByText(/API unreachable/)).toBeInTheDocument();
  });
});

describe("token gate", () => {
  it("asks for a token before rendering any authenticated route", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    expect(await screen.findByLabelText(/Mint one with/)).toBeInTheDocument();
    expect(screen.queryByText("SIGNED IN")).toBeNull();
  });

  it("renders the route once a token is supplied, and forgets it on sign-out", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    await userEvent.type(await screen.findByLabelText(/Mint one with/), "USR_A_OWNER.sig");
    await userEvent.click(screen.getByRole("button", { name: /Use token/ }));
    expect(await screen.findByText("SIGNED IN")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Sign out/ }));
    await waitFor(() => expect(screen.queryByText("SIGNED IN")).toBeNull());
    expect(localStorage.getItem("merchantops.token")).toBeNull();
  });

  it("stores the token as a password field, not in plain view", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    expect(await screen.findByLabelText(/Mint one with/)).toHaveAttribute("type", "password");
  });
});

describe("page scaffolding", () => {
  it("exposes a main landmark and a skip link", async () => {
    health.mockResolvedValue(OK);
    renderApp();
    // Without these, a keyboard user tabs the whole header on every navigation.
    expect(await screen.findByRole("link", { name: "Skip to content" }))
      .toHaveAttribute("href", "#main");
    expect(document.querySelector("main#main")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sections" })).toBeInTheDocument();
  });
});

describe("acting identity", () => {
  it("shows who the server says you are, not who the token claims", async () => {
    health.mockResolvedValue(OK);
    me.mockResolvedValue(ANALYST);
    localStorage.setItem("merchantops.token", "USR_A_ANALYST.sig");
    renderApp();
    expect(await screen.findByText("USR_A_ANALYST")).toBeInTheDocument();
    expect(screen.getByText(/analyst · MERCH_A/)).toBeInTheDocument();
  });
});

describe("the task rail", () => {
  beforeEach(() => localStorage.setItem("merchantops.token", "t"));

  it("keeps recently opened tasks one click away from every page", async () => {
    health.mockResolvedValue(OK);
    localStorage.setItem("merchantops.recent", JSON.stringify(
      [{ id: "TASK_A", request: "Why did revenue drop this week?", status: "COMPLETED" }]));
    renderApp();
    const link = await screen.findByRole("link", { name: /TASK_A/ });
    expect(link).toHaveAttribute("href", "/tasks/TASK_A");
    expect(screen.getByText("Why did revenue drop this week?")).toBeInTheDocument();
  });

  it("says the list is local rather than letting its placement imply a record", async () => {
    // A rail pinned beside every page looks authoritative. This one is not: the
    // audit trail is server-side, and the rail has to say so itself.
    health.mockResolvedValue(OK);
    localStorage.setItem("merchantops.recent", JSON.stringify(
      [{ id: "TASK_A", request: "anything" }]));
    renderApp();
    expect(await screen.findByText(/the audit trail is server-side/)).toBeInTheDocument();
  });

  it("is absent before sign-in, when there is nothing to navigate to", async () => {
    localStorage.removeItem("merchantops.token");
    health.mockResolvedValue(OK);
    renderApp();
    await screen.findByLabelText(/Mint one with/);
    expect(screen.queryByRole("complementary", { name: "Recent tasks" })).toBeNull();
  });
});

describe("starting the next investigation", () => {
  beforeEach(() => localStorage.setItem("merchantops.token", "t"));

  it("offers a way to start one from the rail, on every page", async () => {
    // It used to be a trip back through the top nav from a task page, which is
    // the page you are most likely to be on when you want the next one.
    health.mockResolvedValue(OK);
    localStorage.setItem("merchantops.recent", JSON.stringify(
      [{ id: "TASK_A", request: "anything", status: "COMPLETED" }]));
    renderApp();
    const link = await screen.findByRole("link", { name: /New investigation/ });
    expect(link).toHaveAttribute("href", "/");
  });

  it("offers it even when nothing has been run yet", async () => {
    health.mockResolvedValue(OK);
    localStorage.removeItem("merchantops.recent");
    renderApp();
    expect(await screen.findByRole("link", { name: /New investigation/ }))
      .toBeInTheDocument();
  });
});
