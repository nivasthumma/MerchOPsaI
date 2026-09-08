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

describe("every value the API can send through <Status>", () => {
  // The four fields that actually feed this component, and their closed sets.
  //
  // Written out rather than derived, because the contract does not carry them:
  // `risk_level`, `plan.status` and `approval_decision` are declared `str` in
  // `app/api/schemas.py`, so `schema.d.ts` says `string | null` and there is
  // nothing to import. Typing them as literals server-side is the better fix
  // and would let this list come from the generated types instead of from
  // here; until then this list is the contract, and it is checked.
  const FEEDS: Record<string, string[]> = {
    // Written from `app/models.TaskStatus`, after the first draft of this list
    // was wrong: it invented QUEUED, EXECUTING and EXPIRED and omitted PENDING,
    // DENIED and ABORTED_BUDGET. The test caught it, which is the argument for
    // the list existing -- and equally the argument for deriving it from the
    // contract once the server declares these as literals.
    "task.status": ["PENDING", "RUNNING", "AWAITING_APPROVAL", "COMPLETED",
                    "DENIED", "REJECTED", "FAILED", "ABORTED_BUDGET"],
    "verification_state": ["SUCCESS", "FAILED", "PARTIAL", "UNKNOWN"],
    "risk_level": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
    "plan.status": ["DRAFT", "ACTIVE", "STOPPED", "ESCALATED", "COMPLETED",
                    "EXPIRED"],
    "approval_decision": ["PENDING", "APPROVED", "REJECTED"],
    "payment.status (uppercased)": ["CAPTURED", "FAILED", "REFUNDED"],
  };

  it.each(Object.entries(FEEDS))("%s is fully described", (_field, values) => {
    expect(values.length).toBeGreaterThan(0);
    const missing = values.filter((v) => !(v in STATUS));
    expect(missing, `no STATUS entry for: ${missing.join(", ")}`).toEqual([]);
  });

  it("does not render risk levels as an unrecognised status", () => {
    // The defect this suite missed. All four risk levels fell through to the
    // fallback and rendered as the same neutral grey dot labelled "An
    // unrecognised status (CRITICAL)". On a console whose job is to make an
    // operator's eye land on the right row, CRITICAL and LOW looked identical.
    for (const level of ["LOW", "MEDIUM", "HIGH", "CRITICAL"]) {
      expect(statusSpec(level).meaning).not.toMatch(/unrecognised/);
    }
    // And they are distinguishable from one another, not merely present.
    const tones = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
      .map((l) => statusSpec(l).tone);
    expect(new Set(tones).size).toBeGreaterThan(1);
  });

  it("still renders an unknown status honestly rather than inventing one", () => {
    // The fallback is correct behaviour and must stay: a status this build has
    // never heard of is shown as received, not guessed at.
    const spec = statusSpec("SOMETHING_NEW");
    expect(spec.tone).toBe("neutral");
    expect(spec.label).toBe("SOMETHING NEW");
    expect(spec.meaning).toMatch(/unrecognised/);
  });
});
