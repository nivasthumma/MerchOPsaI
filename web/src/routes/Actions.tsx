// The Action Center — plan P0-03, P0-04, P1-04.
//
// A first-class financial operations queue, in the five sections the plan
// names and in its order. The order is the design: someone opening this page
// during an incident is looking for the thing waiting on them, and that is
// always the approval queue.
//
// The UNKNOWN section is not a status list. P0-04 is explicit that UNKNOWN is
// unresolved financial *work*, so each row carries age, amount, provider,
// external reference, last known state, attempts, last check, next retry,
// escalation, owner and the incident it came from — and the three actions that
// resolve it: Reverify, Investigate, Escalate.
//
// Nothing on this page decides anything about money. Every button posts to an
// endpoint and renders what comes back; the client never marks an action
// resolved, never predicts an outcome, and never shows optimistic success
// (P1-14).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { ActionCenter as ActionCenterData, ActionRow, PendingApprovalRow } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, When,
} from "../components/Bits";
import { LiveBar } from "../components/LiveBar";
import { Status, StatusCount } from "../components/Status";
import { useToast } from "../components/Toast";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

// The plan's recommended cadence for actions is 2–5 seconds. This is the
// fastest screen in the app because it is the one where a state change means
// money moved.
const INTERVAL_MS = 4000;

type SectionKey = "awaiting_approval" | "executing" | "unknown" | "escalated"
                | "recently_completed";

const TITLES: Record<SectionKey, { title: string; sub: string; status: string }> = {
  awaiting_approval: {
    title: "Awaiting approval", status: "AWAITING_APPROVAL",
    sub: "Policy requires a person. Nothing has reached the provider and nothing will until someone decides.",
  },
  executing: {
    title: "Executing", status: "RUNNING",
    sub: "Claimed or submitted. The outcome has not been read back yet — the provider accepting is not the business outcome.",
  },
  unknown: {
    title: "Unknown", status: "UNKNOWN",
    sub: "Unresolved financial work. The system is re-reading provider state on a schedule; it never retries the action.",
  },
  escalated: {
    title: "Escalated", status: "ESCALATED",
    sub: "Automatic reconciliation is exhausted. These need a person.",
  },
  recently_completed: {
    title: "Recently completed", status: "SUCCESS",
    sub: "Settled either way, verified against the provider.",
  },
};

export default function Actions() {
  const [params, setParams] = useSearchParams();
  const [drawer, setDrawer] = useState<ActionRow | null>(null);
  const toast = useToast();

  const focus = (params.get("section") ?? "") as SectionKey | "";

  const live = useLiveRefresh<ActionCenterData>(
    () => api.actionCenter(), { intervalMs: INTERVAL_MS });
  const d = live.data;

  const setFocus = (next: SectionKey | "") => {
    const p = new URLSearchParams(params);
    if (next) p.set("section", next); else p.delete("section");
    setParams(p, { replace: true });
  };

  const sections = useMemo<SectionKey[]>(() => {
    // The server publishes the section list and its order. Taking it from the
    // response rather than repeating it here means a section added server-side
    // cannot be silently dropped by a client that forgot to render it.
    const all = (d?.sections ?? Object.keys(TITLES)) as SectionKey[];
    return focus ? all.filter((s) => s === focus) : all;
  }, [d, focus]);

  const act = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      toast({ tone: "ok", title: `${label} done.` });
      await live.refresh();
    } catch (e) {
      // The failure is reported as itself. A financial action whose result is
      // unclear must never be smoothed into "something went wrong".
      toast({ tone: "danger", title: `${label} failed`,
              body: (e as ApiError).message });
      await live.refresh();
    }
  }, [toast, live]);

  return (
    <>
      <SectionHead title="Action Center">
        Every financial action this system has taken or is waiting to take.
      </SectionHead>
      <LiveBar live={live} what="the queue" />
      {live.error && !d ? <ErrorBanner error={live.error} /> : null}

      {!d ? <Skeleton rows={8} /> : (
        <>
          <nav className="chips" aria-label="Sections">
            <button className={`chip ${focus ? "" : "info"}`}
                    aria-pressed={!focus} onClick={() => setFocus("")}>
              All <b>{Object.values(d.counts).reduce((a, b) => a + b, 0)}</b>
            </button>
            {(d.sections as SectionKey[]).map((s) => (
              <button key={s} onClick={() => setFocus(focus === s ? "" : s)}
                      aria-pressed={focus === s}
                      style={{ border: "none", background: "none", padding: 0 }}>
                <StatusCount status={TITLES[s].status} count={d.counts[s]}>
                  {TITLES[s].title}
                </StatusCount>
              </button>
            ))}
          </nav>

          {sections.map((key) => (
            <Section key={key} k={key} d={d} onOpen={setDrawer} act={act} />
          ))}
        </>
      )}

      {drawer ? (
        <ActionDrawer row={drawer} onClose={() => setDrawer(null)}
                      policy={d?.reconciliation_policy} act={act} />
      ) : null}
    </>
  );
}

function Section({ k, d, onOpen, act }: {
  k: SectionKey; d: ActionCenterData;
  onOpen: (r: ActionRow) => void;
  act: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const meta = TITLES[k];
  const rows = d[k];

  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h3 className="card-title" style={{ margin: 0 }}>
          {meta.title} <span className="count">{rows.length}</span>
        </h3>
      </div>
      <p className="sub">{meta.sub}</p>

      {rows.length === 0 ? (
        <Empty>{emptyCopy(k)}</Empty>
      ) : k === "awaiting_approval" ? (
        <ApprovalTable rows={rows as PendingApprovalRow[]} />
      ) : (
        <ActionTable rows={rows as ActionRow[]} kind={k} onOpen={onOpen} act={act}
                     policy={d.reconciliation_policy} />
      )}
    </section>
  );
}

/** P1-15: every empty state says what happened and what to do next. "No rows"
 *  is a fact about a query, not an answer to a question anybody had. */
function emptyCopy(k: SectionKey): string {
  switch (k) {
    case "awaiting_approval":
      return "Nothing is waiting on a human decision. Actions that policy gates "
           + "appear here the moment they are proposed.";
    case "executing":
      return "No action is in flight. This fills while a refund or payment link "
           + "is being placed and empties as soon as the outcome is read back.";
    case "unknown":
      return "Every action has a settled outcome. Nothing is unresolved.";
    case "escalated":
      return "Automatic reconciliation has settled everything it was given. "
           + "Nothing needs a person.";
    case "recently_completed":
      return "No action has settled yet.";
  }
}

function ApprovalTable({ rows }: { rows: PendingApprovalRow[] }) {
  return (
    <div className="table-wrap">
      <table className="stacked" aria-label="Approvals awaiting a decision">
        <thead>
          <tr>
            <th scope="col">Approval</th><th scope="col">Action</th>
            <th scope="col">Amount</th><th scope="col">Risk</th>
            <th scope="col">Signatures</th><th scope="col">Window</th>
            <th scope="col">Investigation</th><th scope="col">Decide</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const amount = Number(
              (r.action_payload as { amount_minor?: number }).amount_minor ?? 0);
            return (
              <tr key={r.approval_id} className={r.expired ? "is-stale" : ""}>
                <td data-label="Approval"><CopyId value={r.approval_id} /></td>
                <td data-label="Action">{r.action_type}</td>
                <td data-label="Amount" className="mono"><Money minor={amount} /></td>
                <td data-label="Risk"><Status status={r.risk_level} compact /></td>
                <td data-label="Signatures" className="mono">{r.signatures} / {r.required_signatures}</td>
                <td data-label="Window">
                  {r.expired
                    ? <Status status="EXPIRED" />
                    : <><span className="muted">expires </span><When iso={r.expires_at} /></>}
                </td>
                <td data-label="Investigation" data-priority="low"><Link to={`/tasks/${r.task_id}`} className="mono">{r.task_id}</Link></td>
                <td>
                  {/* Deliberately not an inline Approve button. Approving is a
                      financial decision and P0-11 makes it a hard gate; it
                      belongs on the page that shows the evidence, not on a row
                      in a list where the evidence is off screen. */}
                  <Link className="linkish" to={`/tasks/${r.task_id}`}>
                    Review evidence →
                  </Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ActionTable({ rows, kind, onOpen, act, policy }: {
  rows: ActionRow[]; kind: SectionKey;
  onOpen: (r: ActionRow) => void;
  act: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  policy: { max_attempts: number; on_exhaustion: string };
}) {
  const reconciling = kind === "unknown" || kind === "escalated";

  return (
    <div className="table-wrap">
      {/* P1-11. `stacked` turns this into one block per action below 760px —
          twelve columns behind a horizontal scrollbar is a scrollbar with a
          table hidden behind it. `data-label` on every cell is what the
          headers become there, and `data-priority="low"` drops the ones an
          operator can read in the drawer instead. Nothing that asserts
          something about money is ever marked low. */}
      <table className="stacked" aria-label="Actions">
        <thead>
          <tr>
            <th scope="col">Action</th>
            <th scope="col">Type</th>
            <th scope="col">Amount</th>
            <th scope="col">Payment</th>
            <th scope="col">Provider ref</th>
            <th scope="col">State</th>
            {reconciling ? <th scope="col">Attempts</th> : null}
            {reconciling ? <th scope="col">Last check</th> : null}
            {reconciling ? <th scope="col">Next retry</th> : null}
            <th scope="col">Age</th>
            <th scope="col">Owner</th>
            <th scope="col">Next</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td data-label="Action">
                <button className="linkish mono" onClick={() => onOpen(r)}
                        aria-label={`Open action ${r.id}`}>{r.id}</button>
              </td>
              <td data-label="Type">{r.action_type}</td>
              <td data-label="Amount" className="mono"><Money minor={r.amount_minor} /></td>
              <td data-label="Payment">
                <span className="mono">{r.target_payment_id}</span>
                {r.environment ? (
                  <span className="muted"> · {r.provider} {r.environment}</span>
                ) : null}
              </td>
              <td data-label="Provider ref" className="mono">
                {r.external_reference
                  ? <CopyId value={r.external_reference} />
                  : <span className="muted" title="No provider reference was issued. That is itself why the outcome is unknown.">—</span>}
              </td>
              <td data-label="State"><Status status={r.verification_state} /></td>
              {reconciling ? (
                <td data-label="Attempts" className="mono"
                    title={`Gives up after ${policy.max_attempts}`}>
                  {r.verify_attempts} / {policy.max_attempts}
                </td>
              ) : null}
              {reconciling ? (
                <td data-label="Last check">{r.last_verified_at ? <When iso={r.last_verified_at} />
                                        : <span className="muted">never</span>}</td>
              ) : null}
              {reconciling ? (
                <td data-label="Next retry">
                  {r.escalated
                    ? <span className="muted" title="Escalated actions are out of the automatic loop.">—</span>
                    : r.next_verify_at ? <When iso={r.next_verify_at} />
                    : <span className="muted">due now</span>}
                </td>
              ) : null}
              <td data-label="Age"><When iso={r.created_at} /></td>
              <td data-label="Owner" data-priority="low" className="mono">{r.owner ?? <span className="muted">—</span>}</td>
              <td>
                <div className="row" style={{ gap: 6 }}>
                  {reconciling ? (
                    <button className="linkish"
                            onClick={() => void act("Re-verify",
                                                    () => api.reverify(r.task_id))}>
                      Reverify
                    </button>
                  ) : null}
                  {r.incident_id ? (
                    <Link className="linkish" to={`/incidents/${r.incident_id}`}>
                      Investigate
                    </Link>
                  ) : (
                    <Link className="linkish" to={`/tasks/${r.task_id}`}>Open</Link>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The action detail drawer — P1-04.
 *
 *  Everything needed to judge whether money moved, on one surface, without
 *  navigating away from the queue. */
function ActionDrawer({ row, onClose, policy, act }: {
  row: ActionRow; onClose: () => void;
  policy?: { max_attempts: number; on_exhaustion: string };
  act: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const panel = useRef<HTMLElement>(null);
  const opener = useRef<Element | null>(null);

  // P1-12. `role="dialog" aria-modal="true"` is a promise to assistive
  // technology that this is a modal, and the promise was not being kept: focus
  // stayed on the row behind it, Escape did nothing, and Tab walked off into a
  // page the dialog claims to have made inert. A keyboard user could open this
  // and not get out of it.
  useEffect(() => {
    opener.current = document.activeElement;
    // Focus the panel itself rather than the first control: the first thing in
    // here is a Close button, and landing on it means a screen reader announces
    // "close" before saying what was opened.
    panel.current?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key !== "Tab" || !panel.current) return;

      const focusable = panel.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input, [tabindex]:not([tabindex="-1"])');
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      // Wrap at both ends, so Tab cannot leave a dialog that says it is modal.
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Back where they came from. Without this, closing the drawer drops focus
      // onto <body> and the next Tab starts from the top of the page.
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [onClose]);

  return (
    <div className="drawer-scrim" onClick={onClose} role="presentation">
      <aside className="drawer" role="dialog" aria-modal="true"
             aria-label={`Action ${row.id}`}
             ref={panel} tabIndex={-1}
             onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>
            <span className="mono">{row.id}</span>
          </h3>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <p className="sub">{row.action_type} · <Status status={row.verification_state} /></p>

        <dl className="kv">
          <Row k="Amount"><span className="mono"><Money minor={row.amount_minor} /></span></Row>
          <Row k="Payment"><span className="mono">{row.target_payment_id}</span></Row>
          <Row k="Customer"><span className="mono">{row.customer_id ?? "—"}</span></Row>
          <Row k="Method">{row.payment_method ?? "—"}</Row>
          <Row k="Provider">
            {row.provider ? `${row.provider} · ${row.environment}` : "—"}
          </Row>
          <Row k="Provider reference">
            {row.external_reference
              ? <CopyId value={row.external_reference} />
              : <span className="muted">none issued</span>}
          </Row>
          <Row k="External payment">
            <span className="mono">{row.external_payment_id ?? "—"}</span>
          </Row>
          <Row k="Approval">
            {row.approval_id
              ? <><CopyId value={row.approval_id} />{" "}
                  {row.risk_level ? <Status status={row.risk_level} compact /> : null}</>
              : <span className="muted">none — this action needed no approval</span>}
          </Row>
          <Row k="Attempts">
            <span className="mono">
              {row.verify_attempts}{policy ? ` / ${policy.max_attempts}` : ""}
            </span>
            {row.escalated ? <> · <Status status="ESCALATED" /></> : null}
          </Row>
          <Row k="Last checked">
            {row.last_verified_at ? <When iso={row.last_verified_at} /> : "never"}
          </Row>
          <Row k="Next retry">
            {row.escalated ? "—" : row.next_verify_at
              ? <When iso={row.next_verify_at} /> : "due now"}
          </Row>
          <Row k="Provider latency">
            {row.provider_latency_ms == null ? "—"
              : <span className="mono">{Math.round(row.provider_latency_ms)}ms</span>}
          </Row>
          <Row k="Verification latency">
            {row.verification_latency_ms == null ? "—"
              : <span className="mono">{Math.round(row.verification_latency_ms)}ms</span>}
          </Row>
          <Row k="Created"><When iso={row.created_at} /></Row>
        </dl>

        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => void act("Re-verify", () => api.reverify(row.task_id))}>
            Reverify
          </button>
          <Link className="linkish" to={`/tasks/${row.task_id}`}>Open investigation</Link>
          {row.incident_id ? (
            <Link className="linkish" to={`/incidents/${row.incident_id}`}>Open incident</Link>
          ) : null}
        </div>

        <p className="sub" style={{ marginBottom: 0 }}>
          Re-verify re-reads provider state. It never re-sends the action — a
          blind retry of an action whose outcome is unknown is the one thing
          this system will not do.
        </p>
      </aside>
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{children}</dd>
    </>
  );
}
