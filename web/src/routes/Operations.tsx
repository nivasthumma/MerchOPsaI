import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EscalatedAction, ReconcileReport } from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip,
  isVerificationState, VerificationPill, When,
} from "../components/Bits";
import { useToast } from "../components/Toast";

export default function Operations() {
  const [rows, setRows] = useState<EscalatedAction[] | null>(null);
  const [report, setReport] = useState<ReconcileReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [params, setParams] = useSearchParams();
  const escalatedOnly = params.get("scope") !== "all";
  const setScope = (scope: "escalated" | "all") => {
    const next = new URLSearchParams(params);
    if (scope === "all") next.set("scope", "all");
    else next.delete("scope");
    setParams(next, { replace: true });
  };
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);
  const [live, setLive] = useState(true);
  const [minAge, setMinAge] = useState(30);
  const [error, setError] = useState<ApiError | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      // 5 is the escalation line; 0 is everything still unsettled, including
      // the actions the sweep has not given up on. Showing only the former
      // leaves an operator blind to work in progress.
      setRows(await api.escalated(escalatedOnly ? 5 : 0));
      setFetchedAt(new Date().toISOString());
    } catch (e) {
      setError(e as ApiError);
    }
  }, [escalatedOnly]);

  useEffect(() => { void load(); }, [load]);

  // This queue changes without anyone touching this tab: a cron sweep settles
  // something, another operator approves a refund. A stale work list is worse
  // than an empty one, because it looks current.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = () => { if (!document.hidden) void load(); };
    const start = () => { if (!timer) timer = setInterval(tick, 15000); };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const onVisibility = () => {
      if (document.hidden) { setLive(false); stop(); }
      else { setLive(true); void load(); start(); }
    };
    setLive(!document.hidden);
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [load]);

  async function sweep() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.reconcile({ minAgeSeconds: minAge });
      setReport(r);
      toast({
        tone: r.escalated > 0 ? "warn" : "ok",
        title: r.scanned === 0 ? "Nothing to sweep" : `Swept ${r.scanned} action(s)`,
        body: r.scanned === 0
          ? "Every action is settled, or too recent to re-read."
          : `${r.settled} settled · ${r.still_unsettled} still unsettled · ${r.escalated} escalated. State was read, never re-issued.`,
      });
      await load();
    } catch (e) {
      const err = e as ApiError;
      setError(err);
      toast({ tone: "danger", title: "Sweep failed", body: err.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Operations</h1>
        <p className="request">
          Everything the agent left unsettled, and the one safe way to settle it.
        </p>
      </div>

      <div role="alert" aria-live="assertive"><ErrorBanner error={error} /></div>

      <div className="card">
        <SectionHead title="Reconciliation sweep" />
        <p className="sub">
          A pending state nobody resolves is not safety, it is deferral. The sweep settles
          actions left UNKNOWN or PARTIAL by <strong>re-reading external state</strong>,
          reconciling by idempotency key. There is no path here that re-issues a financial
          action — a blind retry of an action with an unknown outcome is the most dangerous
          thing this system could do.
        </p>
        <div className="row">
          <button className="primary" onClick={sweep} disabled={busy} aria-busy={busy}>
            {busy ? "Sweeping…" : "Run sweep"}
          </button>
          {busy ? <Busy>re-reading external state</Busy> : null}
        </div>

        <details style={{ marginTop: 12 }}>
          <summary className="muted" style={{ fontSize: 12.5, cursor: "pointer" }}>
            Minimum age: {minAge}s
          </summary>
          <p className="sub" style={{ marginTop: 8 }}>
            Actions younger than this are skipped. A refund submitted seconds ago may
            simply not have propagated, and re-reading it immediately burns an attempt —
            five of those and a perfectly healthy action is escalated to a human. Lower it
            only when you are deliberately exercising the path, as in a demo.
          </p>
          <div className="row">
            {[0, 5, 30].map((s) => (
              <button key={s} aria-pressed={minAge === s} onClick={() => setMinAge(s)}
                      className={minAge === s ? "" : undefined}>
                {s}s{s === 30 ? " (default)" : ""}
              </button>
            ))}
            {minAge < 30 ? (
              <span className="pill warn">guard relaxed</span>
            ) : null}
          </div>
        </details>

        {report ? (
          <>
            <StatStrip items={[
              ["Scanned", report.scanned],
              ["Settled", report.settled],
              ["Still unsettled", report.still_unsettled],
              ["Escalated", report.escalated],
              ["Too recent", report.skipped_too_recent],
            ]} />
            {report.scanned === 0 ? (
              <p className="sub">
                Nothing to scan. Actions younger than 30 seconds are skipped deliberately:
                a refund submitted moments ago may simply not have propagated, and burning
                an attempt on it can escalate a healthy action.
              </p>
            ) : null}
            {report.details.length ? (
              <>
                <h3>What the sweep read</h3>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>Action</th><th>From</th><th>To</th><th>Attempt</th>
                          <th>Reference</th><th /></tr>
                    </thead>
                    <tbody>
                      {report.details.map((d, i) => (
                        <tr key={`${d.action_id}-${i}`}>
                          <td className="mono">
                            {d.task_id
                              ? <Link to={`/tasks/${d.task_id}`} title="Open the task">
                                  {d.action_id}
                                </Link>
                              : d.action_id}
                          </td>
                          <td>{d.from ?? "—"}</td>
                          <td>
                            {isVerificationState(d.to)
                              ? <VerificationPill state={d.to} />
                              : <span className="mono muted">{d.to ?? "—"}</span>}
                          </td>
                          <td>{d.attempt ?? "—"}</td>
                          <td className="mono">{d.external_reference ?? "—"}</td>
                          <td>
                            {d.escalated ? <span className="pill danger">escalated</span> : null}
                            {d.error ? <span className="pill danger" title={d.error}>error</span> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="card">
        <SectionHead title="Operator queue" count={rows ? `${rows.length}` : undefined}>
          <span className="muted" style={{ fontSize: 12 }}>
            {fetchedAt ? <>read <When iso={fetchedAt} /></> : null}
            {live ? <span className="pill ok" style={{ marginLeft: 8 }}>live</span>
                  : <span className="pill neutral" style={{ marginLeft: 8 }}>paused</span>}
          </span>
          <div className="filters" style={{ margin: 0 }}>
            <button aria-pressed={escalatedOnly} onClick={() => setScope("escalated")}>
              Escalated
            </button>
            <button aria-pressed={!escalatedOnly} onClick={() => setScope("all")}>
              All unsettled
            </button>
          </div>
        </SectionHead>
        <p className="sub">
          {escalatedOnly
            ? "Actions the sweep could not settle within its attempt limit — five tries. Escalated rather than swept forever, so nothing sits unresolved and invisible."
            : "Every action still UNKNOWN or PARTIAL, including those the sweep has not given up on. Anything below five attempts will be retried automatically; it is here so work in progress is visible, not because it needs a human yet."}
        </p>
        {rows === null ? <Skeleton rows={2} />
          : rows.length === 0 ? (
            <Empty>
              {escalatedOnly
                ? "Nothing escalated — no action has exhausted its attempts. This queue being empty is the expected condition, not a missing feature."
                : "Nothing unsettled — every action reached SUCCESS, FAILED or PARTIAL and stayed there."}
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Action</th><th>Task</th><th>Payment</th><th>Amount</th>
                    <th>Attempts</th><th>State</th><th>Why</th><th>Last read</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td><CopyId value={r.id} label="action id" /></td>
                      <td className="mono"><Link to={`/tasks/${r.task_id}`}>{r.task_id}</Link></td>
                      <td className="mono">
                        {r.target_payment_id ?? "—"}
                        {r.external_payment_id ? (
                          <div className="muted" style={{ fontSize: 11 }}>{r.external_payment_id}</div>
                        ) : null}
                      </td>
                      <td><Money minor={r.amount_minor} /></td>
                      <td>{r.verify_attempts}</td>
                      <td><VerificationPill state={r.verification_state} /></td>
                      <td style={{ maxWidth: 320 }}>
                        {r.verification_detail ? (
                          <span className="muted" style={{ fontSize: 12.5 }}>
                            {r.verification_detail.reason}
                          </span>
                        ) : <span className="muted">—</span>}
                      </td>
                      <td className="muted"><When iso={r.updated_at} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </div>
    </>
  );
}
