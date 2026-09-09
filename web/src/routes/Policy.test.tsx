// The fixture is a live `/policy` response for the seeded merchant: five
// controls, of which one is editable and one is stored-and-ignored.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PolicyView } from "../api/types";
import Policy from "./Policy";
import policyFixture from "../test-fixtures/policy.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { policy: vi.fn(), setRefundLimit: vi.fn() } };
});

vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const POLICY = policyFixture as unknown as PolicyView;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.policy.mockResolvedValue(POLICY);
});

const show = async () => {
  render(<Policy />);
  await screen.findByText("Refund limit");
};

describe("policy", () => {
  it("shows the limit in force and the default it came from", async () => {
    await show();
    // Effective alone cannot distinguish "chosen" from "inherited".
    expect(screen.getByText("In force")).toBeInTheDocument();
    expect(screen.getByText("Platform default")).toBeInTheDocument();
  });

  it("lists the controls a merchant cannot change", async () => {
    await show();
    // A page showing only the one knob implies that is all policy is.
    expect(screen.getByText(/Not configurable here/)).toBeInTheDocument();
    expect(screen.getByText("Two signatures on high risk")).toBeInTheDocument();
    expect(screen.getByText("Risk is computed, never declared")).toBeInTheDocument();
  });

  it("calls out the control that is stored and does nothing", async () => {
    await show();
    // `auto_approve_below_minor` sits in every merchant's config and is read by
    // no code path. Somebody setting it would believe small refunds
    // auto-approve. Omitting it is how it stayed invisible.
    const row = screen.getByText("Auto-approve below").closest("li") as HTMLElement;
    expect(within(row).getByText(/Stored and ignored/)).toBeInTheDocument();
    expect(within(row).getByText(/NOT IMPLEMENTED/)).toBeInTheDocument();
  });

  it("sends the limit in paise, not rupees", async () => {
    await show();
    mocked.setRefundLimit.mockResolvedValue({
      merchant_id: "MERCH_A", key: "refund_limit_minor",
      before: 500000, after: 250000, changed: true,
    });

    const field = screen.getByLabelText("New limit (₹)");
    await userEvent.clear(field);
    await userEvent.type(field, "2500");
    await userEvent.click(screen.getByRole("button", { name: "Change limit" }));

    // The engine reads paise. A screen that sent rupees would silently divide
    // every limit by a hundred.
    expect(mocked.setRefundLimit).toHaveBeenCalledWith(250000);
  });

  it("clears the override with null rather than zero", async () => {
    await show();
    mocked.setRefundLimit.mockResolvedValue({
      merchant_id: "MERCH_A", key: "refund_limit_minor",
      before: 500000, after: null, changed: true,
    });

    await userEvent.click(
      screen.getByRole("button", { name: "Use the platform default" }));

    // Zero is a real limit that refuses every refund. Producing it by accident
    // when somebody meant "back to default" would be a quiet outage.
    expect(mocked.setRefundLimit).toHaveBeenCalledWith(null);
  });

  it("does not offer to clear an override that does not exist", async () => {
    mocked.policy.mockResolvedValue({
      merchant_id: "MERCH_A",
      controls: POLICY.controls.map((c) =>
        c.key === "refund_limit_minor" ? { ...c, overridden: false } : c),
    } satisfies PolicyView);
    await show();

    expect(screen.getByRole("button", { name: "Use the platform default" }))
      .toBeDisabled();
  });

  it("shows a refused change beside the page rather than replacing it", async () => {
    await show();
    mocked.setRefundLimit.mockRejectedValue(
      new ApiError(400, "A refund limit cannot be negative.", "invalid_limit"));

    await userEvent.click(screen.getByRole("button", { name: "Change limit" }));

    expect(await screen.findByText(/cannot be negative/)).toBeInTheDocument();
    expect(screen.getByText("Refund limit")).toBeInTheDocument();
  });

  it("explains the owner-only refusal instead of reporting a fault", async () => {
    mocked.policy.mockRejectedValue(
      new ApiError(403, "Policy requires the owner role.", "role_required"));
    render(<Policy />);

    expect(await screen.findByText(/requires the/)).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });
});
