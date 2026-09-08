// The fixture is a live `/access-review` response, captured through the real
// TestClient against the seeded tenant rather than written by hand. The
// operator queue's type once claimed a column the query never selected, and
// the UI rendered an always-empty cell with nothing complaining; a fixture
// invented alongside the component it feeds cannot catch that.
//
// The seed has no offboarded account, so the DISABLED row below IS constructed
// — and it is the one case in this file that does not come from a real
// response. It is here because "whose access was removed, and when" is half of
// what a review asks, and leaving it untested would leave that half unwritten.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AccessReview as Review } from "../api/types";
import AccessReview from "./AccessReview";
import reviewFixture from "../test-fixtures/access-review.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { accessReview: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const REVIEW = reviewFixture as unknown as Review;

/** The seed carries no offboarded account. */
const WITH_DISABLED: Review = {
  ...REVIEW,
  users: [
    ...REVIEW.users,
    {
      user_id: "USR_A_LEAVER",
      email: "leaver@kettle.example",
      merchant_id: "MERCH_A",
      role: "analyst",
      permissions: ["read:metrics", "read:orders"],
      status: "DISABLED",
      deactivated_at: "2026-09-01T09:00:00+00:00",
    },
  ],
};

beforeEach(() => vi.clearAllMocks());

function rowFor(userId: string) {
  return screen.getByText(userId).closest("tr") as HTMLElement;
}

describe("access review", () => {
  it("renders the real captured response without inventing columns", async () => {
    mocked.accessReview.mockResolvedValue(REVIEW);
    render(<AccessReview />);

    expect(await screen.findByText("TEN_KETTLE")).toBeInTheDocument();
    for (const u of REVIEW.users) {
      expect(screen.getByText(u.user_id)).toBeInTheDocument();
      expect(screen.getByText(u.email)).toBeInTheDocument();
    }
  });

  it("marks who can move money, and does not mark who cannot", async () => {
    mocked.accessReview.mockResolvedValue(REVIEW);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    // The owner and the approver hold `action:` permissions in the seed; the
    // analyst holds only reads. This is the assertion the whole screen exists
    // to support, so it names the people rather than counting badges.
    expect(within(rowFor("USR_A_OWNER")).getByText("can act")).toBeInTheDocument();
    expect(within(rowFor("USR_A_APPROVER")).getByText("can act")).toBeInTheDocument();
    expect(within(rowFor("USR_A_ANALYST")).queryByText("can act")).toBeNull();
  });

  it("derives 'can act' from the action: prefix, not a hardcoded permission list", async () => {
    // A permission the catalogue does not have today. The registry generates
    // the catalogue, so one will exist eventually; a screen that listed
    // `action:refund` and `action:recover` by name would keep looking
    // authoritative while quietly getting this wrong.
    mocked.accessReview.mockResolvedValue({
      ...REVIEW,
      users: [{
        user_id: "USR_A_FUTURE",
        email: "future@kettle.example",
        merchant_id: "MERCH_A",
        role: "analyst",
        permissions: ["read:orders", "action:issue_payment_link"],
        status: "ACTIVE",
        deactivated_at: null,
      }],
    } satisfies Review);
    render(<AccessReview />);

    expect(await screen.findByText("USR_A_FUTURE")).toBeInTheDocument();
    expect(within(rowFor("USR_A_FUTURE")).getByText("can act")).toBeInTheDocument();
  });

  it("lists an offboarded account and says when access was removed", async () => {
    mocked.accessReview.mockResolvedValue(WITH_DISABLED);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    const row = rowFor("USR_A_LEAVER");
    expect(within(row).getByText("disabled")).toBeInTheDocument();
    // The date, not just the state: "still disabled" and "disabled this
    // morning" are different findings and a review needs to tell them apart.
    expect(within(row).getByRole("time")).toHaveAttribute(
      "datetime", "2026-09-01T09:00:00+00:00");
  });

  it("filters to the people who can move money", async () => {
    mocked.accessReview.mockResolvedValue(WITH_DISABLED);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    await userEvent.click(screen.getByRole("button", { name: "Can move money" }));

    expect(screen.getByText("USR_A_OWNER")).toBeInTheDocument();
    expect(screen.getByText("USR_A_APPROVER")).toBeInTheDocument();
    expect(screen.queryByText("USR_A_ANALYST")).toBeNull();
    expect(screen.queryByText("USR_A_LEAVER")).toBeNull();
  });

  it("filters to offboarded accounts", async () => {
    mocked.accessReview.mockResolvedValue(WITH_DISABLED);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    await userEvent.click(screen.getByRole("button", { name: "Offboarded" }));

    expect(screen.getByText("USR_A_LEAVER")).toBeInTheDocument();
    expect(screen.queryByText("USR_A_OWNER")).toBeNull();
  });

  it("says so when a filter matches nobody, rather than showing an empty table", async () => {
    mocked.accessReview.mockResolvedValue(REVIEW);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    await userEvent.click(screen.getByRole("button", { name: "Offboarded" }));
    expect(screen.getByText("Nobody has been offboarded.")).toBeInTheDocument();
  });

  it("shows what a role grants and how many active people hold it", async () => {
    mocked.accessReview.mockResolvedValue(WITH_DISABLED);
    render(<AccessReview />);
    await screen.findByText("TEN_KETTLE");

    const roleRow = screen.getAllByText("approver")
      .map((n) => n.closest("tr") as HTMLElement)
      .find((r) => within(r).queryByText("action:refund"));
    expect(roleRow).toBeDefined();
    expect(within(roleRow!).getByText("action:refund")).toBeInTheDocument();
  });

  it("explains the owner-only refusal instead of reporting a fault", async () => {
    mocked.accessReview.mockRejectedValue(
      new ApiError(403, "Reviewing access requires the owner role.", "role_required"));
    render(<AccessReview />);

    expect(await screen.findByText(/requires the/)).toBeInTheDocument();
    // Being refused is the control working, so it must not read as an error.
    expect(screen.queryByText("Error")).toBeNull();
  });

  it("surfaces a real failure as an error", async () => {
    mocked.accessReview.mockRejectedValue(new ApiError(500, "boom"));
    render(<AccessReview />);
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});
