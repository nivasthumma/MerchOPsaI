import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { IncidentSummary } from "../api/types";
import {
  Busy, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";
import { useToast } from "../components/Toast";

/** MerchantOps §13 / §50.
 *
 *  The dashboard already linked to individual incidents and there was no page
 *  that listed them, so the only route into an incident was a table cell on
 *  another screen. An operations console whose work queue is a column of
 *  somebody else's report is not a queue.
 *
 *  Ordered by revenue at risk: the biggest problem is the one to open first.
 */
export default function Incidents() {
  const [rows, setRows] = useState<IncidentSummary[] | null>(null);
  const [atRisk, setAtRisk] = useState(0);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const r = await api.incidents();
      setRows(r.incidents);
      setAtRisk(r.total_revenue_at_risk_minor);
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function runDetection() {
    setBusy(true);
    try {
      const r = await api.detect();
      // Idempotent by construction, and saying so matters: an operator who
      // presses this twice should be told nothing new appeared rather than
      // left wondering whether it did.
      toast(r.incidents_created
        ? { tone: "ok", title: `Detected ${r.incidents_created} new incident(s)`,
            body: `${r.anomalies_found} anomalies found in ${r.duration_ms} ms.` }
        : { tone: "ok", title: "Nothing new",
            body: `${r.already_known} anomaly/anomalies already known. Detection is `
                  + `idempotent — a second sweep over the same window raises nothing.` });
      await load();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (error) return <ErrorBanner error={error} />;
  if (!rows) return <Skeleton rows={5} />;

  return (
    <div className="incidents">
      <SectionHead title="Incidents" count={rows.length}>
        <button onClick={runDetection} disabled={busy}>
          {busy ? <Busy>detecting</Busy> : "Run detection"}
        </button>
      </SectionHead>

      <StatStrip items={[
        ["Open", rows.length],
        ["Revenue at risk", <Money key="r" minor={atRisk} />],
      ]} />

      {rows.length === 0 ? (
        <Empty>
          No open incidents. Detection is a sweep, not a daemon — run it to look
          again.
        </Empty>
      ) : (
        <table className="grid" aria-label="Open incidents">
          <thead>
            <tr>
              <th>Incident</th><th>Type</th><th>Severity</th><th>Status</th>
              <th className="num">At risk</th><th>Started</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i.id}>
                <td><Link to={`/incidents/${i.id}`}>{i.title}</Link></td>
                <td>{i.type.toLowerCase().replace(/_/g, " ")}</td>
                <td data-sev={i.severity}>{i.severity}</td>
                <td>{i.status.toLowerCase().replace(/_/g, " ")}</td>
                <td className="num"><Money minor={i.revenue_at_risk_minor} /></td>
                <td><When iso={i.started_at} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
