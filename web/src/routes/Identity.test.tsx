// The `/sso` and `/scim/tokens` fixtures are live responses, and in the seeded
// tenant both are EMPTY — SSO is unconfigured and no provisioning token exists.
// That makes the empty states the real captured case and the populated ones
// constructed, which is the opposite of the other admin screens and is marked
// where it happens.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RoleList, ScimTokenList, SsoConfig } from "../api/types";
import Identity from "./Identity";
import ssoFixture from "../test-fixtures/admin-sso.json";
import tokensFixture from "../test-fixtures/admin-scim-tokens.json";
import rolesFixture from "../test-fixtures/admin-roles.json";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      sso: vi.fn(), scimTokens: vi.fn(), roles: vi.fn(),
      createScimToken: vi.fn(), revokeScimToken: vi.fn(),
    },
  };
});

vi.mock("../components/Toast", () => ({ useToast: () => vi.fn() }));

const { api, ApiError } = await import("../api/client");
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const SSO = ssoFixture as unknown as SsoConfig;
const TOKENS = tokensFixture as unknown as ScimTokenList;
const ROLES = rolesFixture as unknown as RoleList;

/** Constructed: the seed configures no identity provider. */
const CONFIGURED: SsoConfig = {
  configured: true,
  issuer: "https://kettle.okta.example",
  client_id: "0oa1b2c3",
  email_domains: ["kettle.example"],
  default_role: "analyst",
  default_merchant_id: "MERCH_A",
  enabled: true,
};

/** Constructed: the seed mints no provisioning tokens. */
const WITH_TOKENS: ScimTokenList = {
  tokens: [
    { id: "SCT_LIVE", name: "Okta production", default_merchant_id: "MERCH_A",
      default_role: "analyst", created_at: "2026-09-01T09:00:00+00:00",
      last_used_at: "2026-09-08T09:00:00+00:00", revoked: false },
    { id: "SCT_OLD", name: "Okta staging", default_merchant_id: "MERCH_A",
      default_role: "analyst", created_at: "2026-08-01T09:00:00+00:00",
      last_used_at: null, revoked: true },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.sso.mockResolvedValue(SSO);
  mocked.scimTokens.mockResolvedValue(TOKENS);
  mocked.roles.mockResolvedValue(ROLES);
});

const show = async () => {
  render(<Identity />);
  await screen.findByText("Single sign-on");
};

describe("identity", () => {
  it("says plainly when no identity provider is configured", async () => {
    // The captured state. An unconfigured tenant is the common case and must
    // read as a fact rather than as a blank panel.
    await show();
    expect(screen.getByText(/Nobody signs in through an identity provider/))
      .toBeInTheDocument();
  });

  it("does not offer to edit the client secret", async () => {
    await show();
    // `GET /sso` never returns it, and a screen that asks for a credential back
    // to change an unrelated field puts it in a form, a history entry and a
    // heap dump for nothing.
    expect(screen.queryByLabelText(/secret/i)).toBeNull();
    expect(screen.getByText(/written once and\s+never read back/)).toBeInTheDocument();
  });

  it("shows the configuration when there is one, without the secret", async () => {
    mocked.sso.mockResolvedValue(CONFIGURED);
    await show();

    expect(screen.getByText("https://kettle.okta.example")).toBeInTheDocument();
    expect(screen.getByText("kettle.example")).toBeInTheDocument();
    expect(screen.queryByText(/client_secret/)).toBeNull();
  });

  // ------------------------------------------------------- provisioning tokens
  it("says so when accounts are created by hand", async () => {
    await show();
    expect(screen.getByText(/No provisioning tokens/)).toBeInTheDocument();
  });

  it("lists revoked tokens rather than dropping them", async () => {
    mocked.scimTokens.mockResolvedValue(WITH_TOKENS);
    await show();

    // "Used until the 4th, then withdrawn" is the question an auditor asks, and
    // a deleted row answers it with silence.
    expect(screen.getByText("Okta staging")).toBeInTheDocument();
    expect(screen.getByText("revoked")).toBeInTheDocument();
  });

  it("calls out a token that has never been used", async () => {
    mocked.scimTokens.mockResolvedValue(WITH_TOKENS);
    await show();

    // Either misconfigured at the IdP or minted and forgotten. Both want noticing.
    const row = screen.getByText("Okta staging").closest("tr") as HTMLElement;
    expect(within(row).getByText("never used")).toBeInTheDocument();
  });

  it("offers no revoke control on an already-revoked token", async () => {
    mocked.scimTokens.mockResolvedValue(WITH_TOKENS);
    await show();

    const live = screen.getByText("Okta production").closest("tr") as HTMLElement;
    const dead = screen.getByText("Okta staging").closest("tr") as HTMLElement;
    expect(within(live).getByRole("button", { name: "Revoke" })).toBeInTheDocument();
    expect(within(dead).queryByRole("button", { name: "Revoke" })).toBeNull();
  });

  it("confirms before revoking, and says what stops working", async () => {
    mocked.scimTokens.mockResolvedValue(WITH_TOKENS);
    await show();
    const confirmed = vi.spyOn(window, "confirm").mockReturnValue(false);

    const live = screen.getByText("Okta production").closest("tr") as HTMLElement;
    await userEvent.click(within(live).getByRole("button", { name: "Revoke" }));

    expect(confirmed).toHaveBeenCalledWith(
      expect.stringContaining("create or deactivate accounts"));
    expect(mocked.revokeScimToken).not.toHaveBeenCalled();
    confirmed.mockRestore();
  });

  it("shows a new provisioning token once, and says it cannot be shown again", async () => {
    await show();
    mocked.createScimToken.mockResolvedValue({
      id: "SCT_NEW", token: "scim_aaaa.bbbb", name: "Okta production",
    });

    await userEvent.type(screen.getByLabelText("Label"), "Okta production");
    await userEvent.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByText("scim_aaaa.bbbb")).toBeInTheDocument();
    expect(screen.getByText("shown once")).toBeInTheDocument();
    expect(screen.getByText(/only time/)).toBeInTheDocument();
  });

  it("explains the owner-only refusal instead of reporting a fault", async () => {
    mocked.sso.mockRejectedValue(
      new ApiError(403, "Identity settings require the owner role.", "role_required"));
    render(<Identity />);

    expect(await screen.findByText(/require the/)).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });
});
