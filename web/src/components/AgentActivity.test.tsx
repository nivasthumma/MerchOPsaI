// Agent activity — plan P0-08.
//
// The component renders what the server built. These tests pin the two things
// a rendering bug would misrepresent: that a blocked step never reads as done,
// and that the state reaches a screen reader as a claim rather than as a glyph.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ActivityStep } from "../api/types";
import { AgentActivity, AiModeBadge, FallbackNotice } from "./AgentActivity";

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

  it("never draws a step with another state's mark", () => {
    // The glyph is the only thing most readers actually look at, and it was
    // asserted nowhere: `data-state` was pinned, the screen-reader text was
    // pinned, and the mark itself was free to say anything. A frontend mutation
    // run replaced ✕ with ✓ and all 311 tests passed — a failed financial step
    // drawn with a tick, which is the single worst thing this list can do.
    //
    // Every distinct state in the fixture, not a sample: a table of marks is
    // exactly where two entries drift into each other.
    render(<AgentActivity steps={STEPS} />);
    const items = screen.getAllByRole("listitem");
    const mark = (i: number) =>
      items[i].querySelector(".act-glyph")!.textContent;

    expect(mark(1)).toBe("✓");   // done
    expect(mark(2)).toBe("✕");   // failed — NOT the done mark
    expect(mark(4)).toBe("⏸");   // blocked
    // And no two states share one. A mark that means two things carries no
    // information, whatever it is.
    const marks = [mark(1), mark(2), mark(4)];
    expect(new Set(marks).size).toBe(marks.length);
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

describe("how the run was produced", () => {
  it("shows the fallback notice for the two fallback modes and no other", () => {
    for (const mode of ["AI_FAILED_FALLBACK", "AI_UNAVAILABLE_FALLBACK"]) {
      const { unmount } = render(<FallbackNotice mode={mode} />);
      expect(screen.getByRole("note")).toHaveTextContent(/This is not a model result/);
      unmount();
    }
    for (const mode of ["AI_SUCCESS", "DETERMINISTIC_ONLY", null, undefined]) {
      const { container, unmount } = render(<FallbackNotice mode={mode} />);
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });

  it("carries the mode in words, not only in colour", () => {
    render(<AiModeBadge mode="DETERMINISTIC_ONLY" />);
    expect(screen.getByText("Deterministic planner (no model configured)"))
      .toBeInTheDocument();
    expect(screen.getByText(/How this run was produced/)).toHaveClass("sr-only");
  });

  it("renders a mode it does not recognise as received rather than guessing", () => {
    render(<AiModeBadge mode="SOMETHING_NEW" />);
    expect(screen.getByText("SOMETHING NEW")).toBeInTheDocument();
  });
});
