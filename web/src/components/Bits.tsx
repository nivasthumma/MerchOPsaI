// Small shared pieces. Kept together because each is a few lines and splitting
// them across files would cost more navigation than it saves.

import { useState } from "react";
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

const STATES = ["SUCCESS", "FAILED", "PARTIAL", "UNKNOWN"] as const;

/** The reconcile report types its from/to as plain strings. Narrow rather than
 *  cast: an unrecognised value should render as itself, not be forced into a
 *  state it is not. */
export function isVerificationState(v: unknown): v is VerificationState {
  return typeof v === "string" && (STATES as readonly string[]).includes(v);
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

/** An id worth copying — every id in this system appears in a log, a trace, or
 *  a support conversation, and retyping `TASK_15ACA98D7B` by hand invites
 *  errors that look like data problems. */
export function CopyId({ value, label }: { value: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="copy"
      title={`Copy ${label ?? "id"}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(
          () => { setDone(true); setTimeout(() => setDone(false), 1200); },
          () => { /* clipboard blocked — the value is still selectable on screen */ },
        );
      }}
    >
      {value}
      <span className="hint">{done ? "copied" : "copy"}</span>
    </button>
  );
}

/** Relative time for scanning, absolute time on hover for the record. */
export function When({ iso }: { iso: string }) {
  const then = new Date(iso);
  const secs = Math.round((Date.now() - then.getTime()) / 1000);
  const rel =
    secs < 45 ? "just now"
    : secs < 3600 ? `${Math.round(secs / 60)}m ago`
    : secs < 86400 ? `${Math.round(secs / 3600)}h ago`
    : then.toLocaleDateString();
  return <time dateTime={iso} title={then.toLocaleString()}>{rel}</time>;
}

export function StatStrip({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="stats">
      {items.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SectionHead(
  { title, count, children }: { title: string; count?: ReactNode; children?: ReactNode },
) {
  return (
    <div className="sec-head">
      <h2>{title}</h2>
      {count != null ? <span className="count">{count}</span> : null}
      {children ? <span className="spacer">{children}</span> : null}
    </div>
  );
}

/** A loading state shaped like the content it replaces, so the page does not
 *  jump when it arrives. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skel" style={{ width: `${100 - i * 12}%` }} />
      ))}
    </div>
  );
}
