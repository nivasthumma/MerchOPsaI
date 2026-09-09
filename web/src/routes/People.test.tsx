// Fixtures are live `/users` and `/roles` responses, captured through the real
// TestClient against the seeded tenant. The operator queue's type once claimed
// a column the query never selected and the UI rendered an always-empty cell;
// a fixture invented alongside the component it feeds cannot catch that.
//
// The seed has no disabled account and exactly one owner, so the cases that
// need otherwise are constructed here — and marked where they are.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RoleList, UserList } from "../api/types";
import People from "./People";
import usersFixture from "../test-fixtures/admin-users.json";
import rolesFixture from "../test-fixtures/admin-roles.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      users: vi.fn(), roles: vi.fn(), createUser: vi.fn(),
      updateUser: vi.fn(), signOutUser: vi.fn(),
    },
  };
});

vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const USERS = usersFixture as unknown as UserList;
const ROLES = rolesFixture as unknown as RoleList;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.users.mockResolvedValue(USERS);
  mocked.roles.mockResolvedValue(ROLES);
});

const show = async () => {
  render(<People />);
  await screen.findByText("owner@kettle.example");
};

const rowFor = (email: string) =>
  screen.getByText(email).closest("tr") as HTMLElement;

describe("people", () => {
  it("renders the captured response without inventing columns", async () => {
    await show();
    for (const u of USERS.users) {
      expect(screen.getByText(u.email)).toBeInTheDocument();
    }
  });

  it("offers every role the server knows, not a hardcoded list", async () => {
    await show();
    const select = screen.getByRole("combobox", { name: "Role for analyst@kettle.example" });
    const offered = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(offered).toEqual(ROLES.roles.map((r) => r.name));
  });

  // ---------------------------------------------------------------- the guard
  it("will not let the last owner be demoted or disabled", async () => {
    // The seed has exactly one owner, which is the case the guard exists for.
    await show();
    const row = rowFor("owner@kettle.example");

    expect(screen.getByRole("combobox", { name: "Role for owner@kettle.example" }))
      .toBeDisabled();
    expect(within(row).getByRole("button", { name: "Disable" })).toBeDisabled();
    // Stated on the control, so the reason arrives before the click rather
    // than as a 409 afterwards.
    expect(screen.getByRole("combobox", { name: "Role for owner@kettle.example" }))
      .toHaveAttribute("title", expect.stringContaining("last active owner"));
  });

  it("allows demotion once a second owner exists", async () => {
    // Constructed: the seed has one owner, and "the guard releases" is the
    // half that an always-one-owner fixture cannot show.
    mocked.users.mockResolvedValue({
      users: [...USERS.users, {
        user_id: "USR_A_OWNER2", email: "second@kettle.example",
        role: "owner", status: "ACTIVE",
        permissions: ["action:refund", "read:orders"],
      }],
    } satisfies UserList);
    render(<People />);
    await screen.findByText("second@kettle.example");

    expect(screen.getByRole("combobox", { name: "Role for owner@kettle.example" }))
      .not.toBeDisabled();
  });

  it("shows a refused change as a refusal, not as a fault", async () => {
    await show();
    mocked.updateUser.mockRejectedValue(
      new ApiError(409, "The last active owner cannot be demoted.", "last_owner"));

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Role for analyst@kettle.example" }),
      "approver");

    // `ErrorBanner` renders a 409 as "Refused". A guard firing is the system
    // working, and reading it as breakage is how people learn to force past it.
    expect(await screen.findByText("Refused")).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });

  // ------------------------------------------------------- sign out ≠ disable
  it("separates ending sessions from ending the account", async () => {
    await show();
    const row = rowFor("analyst@kettle.example");

    await userEvent.click(within(row).getByRole("button", { name: "Sign out" }));

    expect(mocked.signOutUser).toHaveBeenCalledWith("USR_A_ANALYST");
    // The account is untouched: this is the lost-laptop action, not the leaver.
    expect(mocked.updateUser).not.toHaveBeenCalled();
  });

  it("confirms before disabling, and says it is the leaver step", async () => {
    await show();
    const confirmed = vi.spyOn(window, "confirm").mockReturnValue(false);

    await userEvent.click(
      within(rowFor("analyst@kettle.example")).getByRole("button", { name: "Disable" }));

    expect(confirmed).toHaveBeenCalledWith(expect.stringContaining("not a"));
    expect(mocked.updateUser).not.toHaveBeenCalled();
    confirmed.mockRestore();
  });

  // ------------------------------------------------------------- the credential
  it("shows a new account's token once, and says it cannot be shown again", async () => {
    await show();
    mocked.createUser.mockResolvedValue({
      user_id: "USR_NEW", email: "new@kettle.example", role: "analyst",
      token: "mo1.aaaa.bbbb",
    });

    await userEvent.type(screen.getByLabelText("Email"), "new@kettle.example");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("mo1.aaaa.bbbb")).toBeInTheDocument();
    expect(screen.getByText("shown once")).toBeInTheDocument();
    // The consequence in words. A credential panel that only says "copy this"
    // is one people close and come back to.
    expect(screen.getByText(/only time/)).toBeInTheDocument();
  });

  it("explains the owner-only refusal instead of reporting a fault", async () => {
    mocked.users.mockRejectedValue(
      new ApiError(403, "Administering people requires the owner role.", "role_required"));
    render(<People />);

    expect(await screen.findByText(/requires the/)).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });
});
