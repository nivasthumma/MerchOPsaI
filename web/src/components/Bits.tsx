// Small shared pieces. Kept together because each is a few lines and splitting
// them across files would cost more navigation than it saves.

import type { ReactNode } from "react";
import type { TaskStatus, VerificationState } from "../api/types";

export function StatusPill({ status }: { status: TaskStatus | string }) {
  const tone =
    status === "COMPLETED" ? "ok"
    : status === "AWAITING_APPROVAL" ? "warn"
    : status === "REJECTED" || status === "FAILED" || status === "ABORTED_BUDGET" ? "danger"
    : "neutral";
  return <span className={`pill ${tone}`}>{status.replace(/_/g, " ")}</span>;
}

export function VerificationPill({ state }: { state: VerificationState | null }) {
  if (!state) return <span className="pill neutral">not verified</span>;
  const tone =
    state === "SUCCESS" ? "ok"
    : state === "UNKNOWN" ? "unknown"
    : state === "PARTIAL" ? "warn"
    : "danger";
  return <span className={`pill ${tone}`}>{state}</span>;
}

/** Minor units are the storage unit everywhere in this system. Rendering them
 *  as rupees without saying so is how off-by-100 bugs reach a refund dialog. */
export function Money({ minor }: { minor: number | null | undefined }) {
  if (minor == null) return <span className="muted">—</span>;
  return (
    <span title={`${minor} minor units`}>
      ₹{(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}
    </span>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as { message?: string; code?: string; isConflict?: boolean };
  const tone = e.isConflict ? "warn" : "danger";
  return (
    <div className={`banner ${tone}`}>
      <strong>{e.isConflict ? "Refused" : "Error"}</strong>
      {e.code ? <code> {e.code}</code> : null} — {e.message ?? String(error)}
    </div>
  );
}

export function Busy({ children = "working" }: { children?: ReactNode }) {
  return <span className="spin muted">{children}…</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
