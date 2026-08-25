import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EscalatedAction, ReconcileReport } from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip,
  VerificationPill, When,
} from "../components/Bits";

export default function Operations() {
  const [rows, setRows] = useState<EscalatedAction[] | null>(null);
  const [report, setReport] = useState<ReconcileReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await api.escalated());
    } catch (e) {
      setError(e as ApiError);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function sweep() {
    setBusy(true);
    setError(null);
    try {
      setReport(await api.reconcile());
      await load();
    } catch (e) {
      setError(e as ApiError);
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
                          <td className="mono">{d.action_id}</td>
                          <td>{d.from ?? "—"}</td>
                          <td><VerificationPill state={d.to as never} /></td>
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
        <SectionHead title="Operator queue" count={rows ? `${rows.length}` : undefined} />
        <p className="sub">
          Actions the sweep could not settle within its attempt limit. Escalated rather
          than swept forever, so nothing sits unresolved and invisible.
        </p>
        {rows === null ? <Skeleton rows={2} />
          : rows.length === 0 ? (
            <Empty>
              Nothing escalated — every action reached a settled state. This queue being
              empty is the expected condition, not a missing feature.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Action</th><th>Task</th><th>Payment</th><th>Amount</th>
                    <th>Attempts</th><th>State</th><th>Last read</th>
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
