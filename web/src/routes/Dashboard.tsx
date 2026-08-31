import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Dashboard as DashboardData } from "../api/types";
import {
  Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";

/** MerchantOps §49 / §50.
 *
 *  The one thing this page must not do is imply that money at risk is money
 *  recovered. §49 ends by saying so outright, so the six figures are shown as a
 *  chain that narrows, each one a subset of the one before it, with the basis
 *  they are measured on stated underneath rather than assumed.
 */
export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.dashboard());
      setFetchedAt(new Date().toISOString());
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (error) return <ErrorBanner error={error} />;
  if (!data) return <Skeleton rows={6} />;

  const r = data.recovery;
  const pct = (v: number) => (r.at_risk_minor ? (100 * v) / r.at_risk_minor : 0);

  // Ordered widest to narrowest. Rendering them as equals — a row of tiles —
  // is what lets a reader take the first number as the headline and the last
  // as a detail, which is the wrong way round.
  const chain: { key: string; label: string; minor: number; note: string }[] = [
    { key: "at_risk", label: "Revenue at risk", minor: r.at_risk_minor,
      note: "measured by detection, across open incidents" },
    { key: "recoverable", label: "Recoverable", minor: r.recoverable_minor,
      note: "the part sitting on transactions we may act on" },
    { key: "attempted", label: "Attempted", minor: r.attempted_minor,
      note: "the part we have actually acted on" },
    { key: "recovered", label: "Recovered", minor: r.recovered_minor,
      note: "confirmed by independent verification" },
  ];

  const outcomes: [string, number, string][] = [
    ["Recovered", r.recovered_minor, "ok"],
    ["Failed", r.failed_minor, "bad"],
    ["Unknown", r.unknown_minor, "warn"],
    ["Outstanding", r.outstanding_minor, "muted"],
  ];

  return (
    <div className="dash">
      {r.invariants_broken.length > 0 && (
        // Shown, never swallowed. A ledger whose figures do not nest is a
        // reporting defect, and hiding it would leave the numbers looking fine.
        <div className="banner danger" role="alert">
          <strong>These figures do not nest.</strong>{" "}
          {r.invariants_broken.join("; ")}. Treat the recovery numbers as
          unreliable until this is resolved.
        </div>
      )}

      <SectionHead title="Revenue recovery">
        {fetchedAt && <span className="muted"><When iso={fetchedAt} /></span>}
      </SectionHead>

      <ol className="ledger-chain" aria-label="Recovery ledger">
        {chain.map((c) => (
          <li key={c.key} data-k={c.key}>
            <div className="ledger-bar">
              <span style={{ width: `${Math.max(pct(c.minor), 1.5)}%` }} />
            </div>
            <dl>
              <dt>{c.label}</dt>
              <dd><Money minor={c.minor} /></dd>
            </dl>
            <p className="muted">{c.note}</p>
          </li>
        ))}
      </ol>

      <p className="muted basis">{r.basis}</p>

      <SectionHead title="Outcomes of what was attempted" />
      <StatStrip items={outcomes.map(([label, minor, tone]) => [
        label, <span key={label} data-tone={tone}><Money minor={minor} /></span>,
      ])} />

      <SectionHead title="At risk by incident" count={r.by_incident.length} />
      {r.by_incident.length === 0
        ? <Empty>No open incidents.</Empty>
        : (
          <table className="grid">
            <thead>
              <tr>
                <th>Incident</th><th>Severity</th><th>Status</th>
                <th className="num">At risk</th>
                <th className="num">Recoverable</th>
                <th className="num">Recovered</th>
              </tr>
            </thead>
            <tbody>
              {r.by_incident.map((i) => (
                <tr key={i.incident_id}>
                  <td>
                    <Link to={`/incidents/${i.incident_id}`}>{i.title}</Link>
                  </td>
                  <td data-sev={i.severity}>{i.severity}</td>
                  <td>{i.status}</td>
                  <td className="num"><Money minor={i.revenue_at_risk_minor} /></td>
                  <td className="num"><Money minor={i.recoverable_minor} /></td>
                  <td className="num"><Money minor={i.recovered_minor} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

      <SectionHead title="At risk by payment method" count={r.by_method.length} />
      {r.by_method.length === 0
        ? <Empty>Nothing planned yet.</Empty>
        : (
          <table className="grid">
            <thead>
              <tr>
                <th>Method</th><th className="num">Candidates</th>
                <th className="num">Recoverable</th><th className="num">Recovered</th>
              </tr>
            </thead>
            <tbody>
              {r.by_method.map((m) => (
                <tr key={m.method}>
                  <td>{m.method}</td>
                  <td className="num">{m.candidates}</td>
                  <td className="num"><Money minor={m.recoverable_minor} /></td>
                  <td className="num"><Money minor={m.recovered_minor} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

      <SectionHead title="Incidents" />
      <StatStrip items={[
        ["Open", data.incidents.open],
        ["Resolved", data.incidents.resolved],
        ...Object.entries(data.incidents.by_status).map(
          ([k, v]) => [k.toLowerCase().replace(/_/g, " "), v] as [string, number]),
      ]} />

      <SectionHead title="Agent activity" />
      <StatStrip items={[
        ["Investigations", data.agent_activity.investigations],
        ["Tool calls", data.agent_activity.tool_calls],
        ["Recommendations", data.agent_activity.recommendations],
        ["Awaiting approval", data.agent_activity.awaiting_approval],
        ["Escalations", data.agent_activity.escalations],
      ]} />
    </div>
  );
}
