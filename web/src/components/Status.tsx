// The status system — plan P1-08.
//
// Thirteen statuses appear across this application, and before this file each
// screen decided for itself what colour SUCCESS was, whether AWAITING_APPROVAL
// was a warning, and what to call ABORTED_BUDGET in prose. `StatusPill` and
// `VerificationPill` in Bits.tsx each held a partial and slightly different
// answer.
//
// So there is one table. Every status has:
//
//   tone       the colour family — and never the only carrier of meaning
//   label      what an operator reads, in business language (§26 of the plan)
//   glyph      a shape, so the state survives colour blindness and greyscale
//   meaning    what it actually asserts, as a title and for screen readers
//
// The last two are the accessibility requirement (P1-12) and the honesty
// requirement in one: a red dot that means "we do not know" and a red dot that
// means "it failed" are the same dot, and those are opposite claims about
// whether money moved.

import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "danger" | "unknown" | "info" | "neutral";

export interface StatusSpec {
  tone: Tone;
  label: string;
  glyph: string;
  meaning: string;
}

// The thirteen the plan enumerates, plus the ones this system actually emits.
// Keyed by the wire value, so a status arriving from the API either matches
// exactly or falls through to `unknownSpec` — which renders it verbatim rather
// than guessing at a tone for a value nobody has defined.
export const STATUS: Record<string, StatusSpec> = {
  // --- financial outcomes: what happened to the money ---
  SUCCESS: {
    tone: "ok", label: "Confirmed", glyph: "✓",
    meaning: "Independently verified at the provider. The money moved.",
  },
  FAILED: {
    tone: "danger", label: "Failed", glyph: "✕",
    meaning: "Verified at the provider as not having taken effect. No money moved.",
  },
  PARTIAL: {
    tone: "warn", label: "Partial", glyph: "◐",
    meaning: "Accepted, but the provider reflects less than was requested.",
  },
  UNKNOWN: {
    tone: "unknown", label: "Unknown", glyph: "?",
    meaning: "The outcome could not be established. This is unresolved "
             + "financial work, not a failure — it is being reconciled.",
  },

  // --- work in progress ---
  PENDING: {
    tone: "neutral", label: "Pending", glyph: "·",
    meaning: "Claimed, not yet submitted to the provider.",
  },
  QUEUED: {
    tone: "neutral", label: "Queued", glyph: "·",
    meaning: "Waiting to start.",
  },
  RUNNING: {
    tone: "info", label: "Running", glyph: "▶",
    meaning: "In progress.",
  },
  SUBMITTED: {
    tone: "info", label: "Submitted", glyph: "▶",
    meaning: "Sent to the provider. The provider accepting is not the same as "
             + "the business outcome being confirmed.",
  },
  VERIFYING: {
    tone: "info", label: "Verifying", glyph: "▶",
    meaning: "Re-reading provider state to establish what actually happened.",
  },
  CONFIRMED: {
    tone: "ok", label: "Confirmed", glyph: "✓",
    meaning: "Verified at the provider.",
  },

  // --- the human gate ---
  AWAITING_APPROVAL: {
    tone: "warn", label: "Awaiting approval", glyph: "⏸",
    meaning: "Policy requires a person to decide. Nothing has been sent to the "
             + "provider and nothing will be until someone approves.",
  },
  COMPLETED: {
    tone: "ok", label: "Completed", glyph: "✓",
    meaning: "Finished.",
  },
  DENIED: {
    tone: "danger", label: "Denied by policy", glyph: "⊘",
    meaning: "Refused by the control plane before any provider call.",
  },
  REJECTED: {
    tone: "danger", label: "Rejected", glyph: "✕",
    meaning: "A person declined it. No provider call was made.",
  },
  EXPIRED: {
    tone: "neutral", label: "Expired", glyph: "⊘",
    meaning: "The approval window closed before anyone decided. It cannot execute.",
  },
  ABORTED_BUDGET: {
    tone: "danger", label: "Stopped — budget", glyph: "⊘",
    meaning: "The run exceeded its bounds and was stopped. Budgets are limits, "
             + "not suggestions.",
  },
  ESCALATED: {
    tone: "danger", label: "Escalated", glyph: "▲",
    meaning: "Automatic reconciliation is exhausted. A person owns this now.",
  },
};

const unknownSpec = (raw: string): StatusSpec => ({
  tone: "neutral",
  label: raw.replace(/_/g, " "),
  glyph: "·",
  // Honest about the gap rather than inventing a meaning for it.
  meaning: `An unrecognised status (${raw}). Rendered as received.`,
});

export function statusSpec(status: string | null | undefined): StatusSpec {
  if (!status) {
    return {
      tone: "neutral", label: "Not verified", glyph: "·",
      meaning: "No verification has been recorded for this action yet.",
    };
  }
  return STATUS[status] ?? unknownSpec(status);
}

/** One status, rendered the same way everywhere.
 *
 *  The glyph is `aria-hidden` and the meaning is the accessible name, so a
 *  screen reader hears "Unknown. The outcome could not be established..."
 *  rather than "question mark". */
export function Status({ status, compact = false }:
                       { status: string | null | undefined; compact?: boolean }) {
  const spec = statusSpec(status);
  return (
    <span className={`pill ${spec.tone}`} title={spec.meaning}>
      <span aria-hidden="true" className="pill-glyph">{spec.glyph}</span>
      <span className="sr-only">{spec.label}. {spec.meaning}</span>
      <span aria-hidden="true">{compact ? spec.label : spec.label}</span>
    </span>
  );
}

/** A count with a status tone, for the section headers of the Action Center.
 *  Zero is rendered as neutral whatever the status: "0 escalated" is good news
 *  and should not be red. */
export function StatusCount({ status, count, children }:
                            { status: string; count: number; children?: ReactNode }) {
  const spec = statusSpec(status);
  const tone = count === 0 ? "neutral" : spec.tone;
  return (
    <span className={`chip ${tone}`} title={spec.meaning}>
      <span aria-hidden="true" className="dot" />
      {children ?? spec.label} <b>{count}</b>
    </span>
  );
}
