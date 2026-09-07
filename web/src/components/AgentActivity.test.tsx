// Agent activity — plan P0-08.
//
// The component renders what the server built. These tests pin the two things
// a rendering bug would misrepresent: that a blocked step never reads as done,
// and that the state reaches a screen reader as a claim rather than as a glyph.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ActivityStep } from "../api/types";
import { AgentActivity } from "./AgentActivity";

const STEPS: ActivityStep[] = [
  { key: "started", label: "Investigation started", state: "done",
    at: "2026-09-07T12:00:00Z", detail: "Why did revenue drop?" },
  { key: "tool:0", label: "Revenue summary read", state: "done",
    at: "2026-09-07T12:00:01Z", detail: "" },
  { key: "tool:1", label: "Payment metrics read", state: "failed",
    at: "2026-09-07T12:00:02Z", detail: "TOOL_TIMEOUT" },
  { key: "policy", label: "Policy evaluated", state: "done",
    at: null, detail: "REQUIRE_APPROVAL" },
  { key: "approval:APR_1", label: "Waiting for approval", state: "blocked",
    at: "2026-09-07T12:00:03Z",
    detail: "0 of 1 signature(s). Nothing has reached the provider." },
];

describe("agent activity", () => {
  it("renders the server's steps in the server's order", () => {
    render(<AgentActivity steps={STEPS} />);
    const items = within(screen.getByRole("list", { name: "Agent activity" }))
      .getAllByRole("listitem");
    expect(items).toHaveLength(STEPS.length);
    expect(items[0]).toHaveTextContent("Investigation started");
    expect(items[4]).toHaveTextContent("Waiting for approval");
  });

  it("distinguishes every state in the markup, not only in colour", () => {
    render(<AgentActivity steps={STEPS} />);
    const items = screen.getAllByRole("listitem");
    expect(items[1]).toHaveAttribute("data-state", "done");
    expect(items[2]).toHaveAttribute("data-state", "failed");
    expect(items[4]).toHaveAttribute("data-state", "blocked");
  });

  it("says what each state means rather than announcing a glyph", () => {
    // A red mark meaning "it failed" and one meaning "we are waiting on you"
    // are different claims and must not sound alike.
    render(<AgentActivity steps={STEPS} />);
    expect(screen.getByText(/did not complete/)).toBeInTheDocument();
    expect(screen.getByText(/waiting on a person/)).toBeInTheDocument();
  });

  it("shows the reason a step failed", () => {
    render(<AgentActivity steps={STEPS} />);
    expect(screen.getByText("TOOL_TIMEOUT")).toBeInTheDocument();
  });

  it("renders a step that has no honest timestamp without inventing one", () => {
    render(<AgentActivity steps={STEPS} />);
    const policy = screen.getAllByRole("listitem")[3];
    expect(policy).toHaveTextContent("Policy evaluated");
    expect(within(policy).queryByRole("time")).toBeNull();
    expect(policy.querySelector("time")).toBeNull();
  });

  it("says why it is empty rather than showing an empty list", () => {
    render(<AgentActivity steps={[]} />);
    expect(screen.getByText(/stays empty until something does/)).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });
});
