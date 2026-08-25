import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EscalatedAction, ReconcileReport } from "../api/types";
import { Busy, Empty, ErrorBanner, Money, VerificationPill } from "../components/Bits";

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
      <div className="card">
        <h2>Reconciliation</h2>
        <p className="sub">
          A pending state nobody resolves is not safety, it is deferral. The sweep settles
          actions left UNKNOWN or PARTIAL by <strong>re-reading external state</strong>,
          reconciling by idempotency key. It has no path that re-issues a financial action
          — a blind retry of an action with an unknown outcome is the most dangerous thing
          this system could do.
        </p>
        <div className="row">
          <button className="primary" onClick={sweep} disabled={busy}>
            {busy ? "Sweeping…" : "Run reconciliation sweep"}
          </button>
          {busy ? <Busy /> : null}
        </div>
        {report ? (
          <dl className="kv" style={{ marginTop: 16 }}>
            {Object.entries(report).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k.replace(/_/g, " ")}</dt>
                <dd>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>

      <ErrorBanner error={error} />

      <div className="card">
        <h2>Operator queue</h2>
        <p className="sub">
          Actions the sweep could not settle after its attempt limit. Escalated rather than
          swept forever, so nothing sits unresolved and invisible.
        </p>
        {rows === null ? <Busy>loading</Busy>
          : rows.length === 0 ? <Empty>Nothing escalated. Every action is settled.</Empty>
          : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Action</th><th>Task</th><th>Type</th><th>Amount</th>
                    <th>Attempts</th><th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.id}</td>
                      <td className="mono"><Link to={`/tasks/${r.task_id}`}>{r.task_id}</Link></td>
                      <td>{r.action_type}</td>
                      <td><Money minor={r.amount_minor} /></td>
                      <td>{r.verify_attempts}</td>
                      <td><VerificationPill state={r.verification_state} /></td>
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
