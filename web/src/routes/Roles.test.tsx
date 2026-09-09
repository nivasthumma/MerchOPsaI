// The fixture is a live `/roles` response: three roles and the permission
// catalogue the tool registry generates.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RoleList } from "../api/types";
import Roles from "./Roles";
import rolesFixture from "../test-fixtures/admin-roles.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { roles: vi.fn(), setRolePermissions: vi.fn() } };
});

vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const ROLES = rolesFixture as unknown as RoleList;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.roles.mockResolvedValue(ROLES);
});

const show = async () => {
  render(<Roles />);
  await screen.findByText("analyst");
};

/** The card for one role — each renders the whole catalogue, so a permission
 *  name alone is ambiguous across cards. */
const cardFor = (role: string) =>
  screen.getByRole("heading", { name: role }).closest(".card") as HTMLElement;

describe("roles", () => {
  it("renders every role and the catalogue the server returned", async () => {
    await show();
    for (const r of ROLES.roles) {
      expect(screen.getByRole("heading", { name: r.name })).toBeInTheDocument();
    }
    // Every permission appears on every role's card, ticked or not: a reader
    // deciding what to grant needs to see what is available, not only what is on.
    const analyst = cardFor("analyst");
    for (const p of ROLES.catalogue) {
      expect(within(analyst).getByText(p.name)).toBeInTheDocument();
    }
  });

  it("marks the roles that can move money, and not the ones that cannot", async () => {
    await show();
    // owner and approver hold `action:` permissions in the seed; analyst does not.
    expect(within(cardFor("owner")).getByText("can move money")).toBeInTheDocument();
    expect(within(cardFor("approver")).getByText("can move money")).toBeInTheDocument();
    expect(within(cardFor("analyst")).queryByText("can move money")).toBeNull();
  });

  it("derives that from the action: prefix, not a hardcoded permission list", async () => {
    // A permission the catalogue does not have today. The registry generates
    // the catalogue, so one will exist eventually.
    mocked.roles.mockResolvedValue({
      roles: [{ name: "future", description: "d",
                permissions: ["action:issue_payment_link"], held_by: 2 }],
      catalogue: [{ name: "action:issue_payment_link", description: "d" }],
    } satisfies RoleList);
    render(<Roles />);
    await screen.findByRole("heading", { name: "future" });

    expect(within(cardFor("future")).getByText("can move money")).toBeInTheDocument();
  });

  it("says how many people a change would reach", async () => {
    await show();
    // Granting to a role held by nine people grants it to nine people, and the
    // decision is made on the card rather than in a heading above it.
    expect(within(cardFor("owner")).getByText(/1 person holds this role/))
      .toBeInTheDocument();
  });

  // ------------------------------------------------------------- staged edits
  it("does not send anything until the change is saved", async () => {
    await show();
    const card = cardFor("analyst");

    await userEvent.click(within(card).getByRole("button", { name: "Edit permissions" }));
    await userEvent.click(within(card).getByRole("checkbox", { name: /action:refund/ }));

    // PUT takes the WHOLE set, so a click-to-save control would make every
    // intermediate state a real grant.
    expect(mocked.setRolePermissions).not.toHaveBeenCalled();
  });

  it("sends the whole set when saved", async () => {
    await show();
    mocked.setRolePermissions.mockResolvedValue({
      name: "analyst", permissions: ["action:refund", "read:metrics", "read:orders"],
      granted: ["action:refund"], revoked: [],
    });
    const card = cardFor("analyst");

    await userEvent.click(within(card).getByRole("button", { name: "Edit permissions" }));
    await userEvent.click(within(card).getByRole("checkbox", { name: /action:refund/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Save permissions" }));

    expect(mocked.setRolePermissions).toHaveBeenCalledWith(
      "analyst", ["action:refund", "read:metrics", "read:orders"]);
  });

  it("abandons a staged change on cancel", async () => {
    await show();
    const card = cardFor("analyst");

    await userEvent.click(within(card).getByRole("button", { name: "Edit permissions" }));
    await userEvent.click(within(card).getByRole("checkbox", { name: /action:refund/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Cancel" }));

    expect(mocked.setRolePermissions).not.toHaveBeenCalled();
    expect(within(card).getByRole("checkbox", { name: /action:refund/ })).not.toBeChecked();
  });

  it("shows a guard firing as a refusal, not as a fault", async () => {
    await show();
    mocked.setRolePermissions.mockRejectedValue(
      new ApiError(409, "A role that nobody holds cannot lose its last permission.",
                   "role_conflict"));
    const card = cardFor("analyst");

    await userEvent.click(within(card).getByRole("button", { name: "Edit permissions" }));
    await userEvent.click(within(card).getByRole("button", { name: "Save permissions" }));

    // 409 is the guard. `ErrorBanner` reads it as "Refused" because the system
    // is working, and reading that as breakage teaches people to force past it.
    expect(await screen.findByText("Refused")).toBeInTheDocument();
  });

  it("keeps a rejected save on the page instead of replacing it", async () => {
    await show();
    // 400 is a genuine fault here rather than a policy refusal: the catalogue
    // came from the server, so sending it a permission it does not know is a
    // bug. It still belongs beside the roles rather than in place of them --
    // an owner mid-edit should not lose what they were looking at.
    mocked.setRolePermissions.mockRejectedValue(
      new ApiError(400, "unknown permission: action:nope", "invalid_permission"));
    const card = cardFor("analyst");

    await userEvent.click(within(card).getByRole("button", { name: "Edit permissions" }));
    await userEvent.click(within(card).getByRole("button", { name: "Save permissions" }));

    expect(await screen.findByText(/unknown permission/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "analyst" })).toBeInTheDocument();
  });

  it("explains the owner-only refusal instead of reporting a fault", async () => {
    mocked.roles.mockRejectedValue(
      new ApiError(403, "Administering roles requires the owner role.", "role_required"));
    render(<Roles />);

    expect(await screen.findByText(/requires the/)).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });
});
