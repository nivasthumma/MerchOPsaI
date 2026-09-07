// The status system — plan P1-08, P1-12.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { STATUS, Status, StatusCount, statusSpec } from "./Status";

describe("one vocabulary", () => {
  it("covers the statuses the plan enumerates", () => {
    // P1-08's list. A status this system emits but does not describe renders
    // with a guessed tone, which for a financial state is a guess about money.
    for (const s of ["SUCCESS", "FAILED", "PARTIAL", "UNKNOWN", "PENDING",
                     "QUEUED", "RUNNING", "AWAITING_APPROVAL", "COMPLETED",
                     "DENIED", "REJECTED", "ESCALATED"]) {
      expect(STATUS[s], `${s} has no entry`).toBeTruthy();
    }
  });

  it("does not colour UNKNOWN as a failure", () => {
    // The distinction the whole design turns on. FAILED asserts no money moved;
    // UNKNOWN asserts nothing. Rendering them alike is the same claim twice.
    expect(statusSpec("UNKNOWN").tone).toBe("unknown");
    expect(statusSpec("FAILED").tone).toBe("danger");
    expect(statusSpec("UNKNOWN").glyph).not.toBe(statusSpec("FAILED").glyph);
  });

  it("renders an unrecognised status as itself rather than guessing", () => {
    const spec = statusSpec("SOMETHING_NEW");
    expect(spec.tone).toBe("neutral");
    expect(spec.label).toBe("SOMETHING NEW");
    expect(spec.meaning).toMatch(/unrecognised/);
  });

  it("distinguishes no verification from a verification that failed", () => {
    expect(statusSpec(null).label).toBe("Not verified");
    expect(statusSpec(null).tone).toBe("neutral");
  });
});

describe("accessibility — P1-12", () => {
  it("carries a shape as well as a colour", () => {
    // A red dot meaning "we do not know" and a red dot meaning "it failed" are
    // the same dot, and those are opposite claims.
    const glyphs = new Set(["SUCCESS", "FAILED", "UNKNOWN", "AWAITING_APPROVAL"]
      .map((s) => statusSpec(s).glyph));
    expect(glyphs.size).toBe(4);
  });

  it("gives a screen reader the meaning, not the glyph", () => {
    render(<Status status="UNKNOWN" />);
    // The glyph is decoration; the sentence is the content.
    expect(screen.getByText(/The outcome could not be established/))
      .toBeInTheDocument();
    expect(screen.getByTitle(/unresolved financial work/i)).toBeInTheDocument();
  });
});

describe("counts", () => {
  it("renders zero as neutral, whatever the status", () => {
    // "0 escalated" is good news and must not be red.
    const { container } = render(<StatusCount status="ESCALATED" count={0} />);
    expect(container.querySelector(".chip")).toHaveClass("neutral");
  });

  it("takes the status tone once there is something to report", () => {
    const { container } = render(<StatusCount status="ESCALATED" count={2} />);
    expect(container.querySelector(".chip")).toHaveClass("danger");
  });
});
