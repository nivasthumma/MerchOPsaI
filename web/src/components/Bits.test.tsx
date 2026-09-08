// These components decide how a financial state *reads*. A verification pill
// that renders UNKNOWN as anything reassuring, or an amount that drops a factor
// of 100, would be a lie told politely.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EFFECTS } from "../api/client";
import { ErrorBanner, Money, StatusPill, VerificationPill } from "./Bits";

describe("VerificationPill", () => {
  it("renders UNKNOWN as UNKNOWN", () => {
    render(<VerificationPill state="UNKNOWN" />);
    const pill = screen.getByText("UNKNOWN");
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveClass("unknown");
    // Specifically not a success tone: an unsettled action must not read as done.
    expect(pill).not.toHaveClass("ok");
  });

  it("distinguishes PARTIAL from SUCCESS", () => {
    const { unmount } = render(<VerificationPill state="PARTIAL" />);
    expect(screen.getByText("PARTIAL")).toHaveClass("warn");
    unmount();
    render(<VerificationPill state="SUCCESS" />);
    expect(screen.getByText("SUCCESS")).toHaveClass("ok");
  });

  it("says 'not verified' rather than nothing when there is no state", () => {
    render(<VerificationPill state={null} />);
    expect(screen.getByText("not verified")).toBeInTheDocument();
  });
});

describe("Money", () => {
  it("converts minor units and keeps the raw value inspectable", () => {
    render(<Money minor={499900} />);
    const el = screen.getByTitle("499900 minor units");
    expect(el).toHaveTextContent("₹4,999.00");
  });

  it("does not render a missing amount as zero", () => {
    render(<Money minor={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("keeps sub-rupee precision", () => {
    render(<Money minor={1} />);
    expect(screen.getByTitle("1 minor units")).toHaveTextContent("₹0.01");
  });
});

describe("StatusPill", () => {
  it("marks an awaiting-approval task as needing attention", () => {
    render(<StatusPill status="AWAITING_APPROVAL" />);
    const pill = screen.getByText("AWAITING APPROVAL");
    expect(pill).toHaveClass("warn");
  });

  it("marks a rejected task as such, not as a neutral outcome", () => {
    render(<StatusPill status="REJECTED" />);
    expect(screen.getByText("REJECTED")).toHaveClass("danger");
  });
});

describe("ErrorBanner", () => {
  it("renders nothing when there is no error", () => {
    const { container } = render(<ErrorBanner error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says what every outcome means for whether anything happened", () => {
    // Walked from `EFFECTS` rather than restated here: the point of deriving
    // `Effect` from that list is that there is one place to add an outcome,
    // and a test carrying its own copy would be a second place to forget.
    //
    // The type makes a missing entry a compile error; this makes an entry that
    // renders nothing a test failure. An operator reading this banner is
    // deciding between pressing the button again and opening the UNKNOWN
    // queue, and a blank consequence gives them nothing to decide with.
    for (const effect of EFFECTS) {
      const { unmount } = render(
        <ErrorBanner error={{ message: "boom", effect }} />);
      const banner = document.querySelector(".banner-consequence");
      expect(banner, `no consequence rendered for "${effect}"`).not.toBeNull();
      expect(banner!.textContent!.length).toBeGreaterThan(20);
      unmount();
    }
  });

  it("shows a refusal with its code, not as a crash", () => {
    render(<ErrorBanner error={{ message: "Approval has expired.", code: "APPROVAL_EXPIRED", isConflict: true }} />);
    expect(screen.getByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("APPROVAL_EXPIRED")).toBeInTheDocument();
  });
});
