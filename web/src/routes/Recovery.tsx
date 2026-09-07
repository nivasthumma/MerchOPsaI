// The Recovery Center — plan P1-02, P1-03, §15.
//
// The eight figures §15 names, the funnel that keeps them honest, and the
// breakdowns that make each one drillable. Everything comes from
// `/recovery/ledger`, which is the single authority on these numbers; this page
// does no arithmetic.
//
// The rule the whole screen is built around is P1-03's: at risk must never
// imply recovered. So the funnel is ordered by the server, drawn in
// proportions of the first stage, and the two figures that are *estimates*
// (expected recovery) are kept visually and verbally separate from the one
// that is a *verified fact* (recovered). §49 keeps them in different columns
// for that reason and nothing here merges them.

import { Link } from "react-router";
import { api } from "../api/client";
import type { Dashboard } from "../api/types";
import {
  Empty, ErrorBanner, Money, SectionHead, Skeleton,
} from "../components/Bits";
import { LiveBar } from "../components/LiveBar";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

const INTERVAL_MS = 10000;

export default function Recovery() {
  const live = useLiveRefresh<Dashboard>(
    () => api.dashboard(), { intervalMs: INTERVAL_MS });
  const d = live.data;

  return (
    <>
      <SectionHead title="Recovery">
        What was at risk, what could be recovered, what was attempted, and what
        actually came back.
      </SectionHead>
      <LiveBar live={live} what="the ledger" />
      {live.error && !d ? <ErrorBanner error={live.error} /> : null}

      {!d ? <Skeleton rows={6} /> : <Ledger d={d} />}
    </>
  );
}

function Ledger({ d }: { d: Dashboard }) {
  const r = d.recovery;

  // §15's eight figures. Grouped by what kind of claim each one is, because
  // that is the distinction an operator has to keep straight and the layout is
  // the cheapest place to carry it.
  const exposure: [string, number, string][] = [
    ["At risk", r.at_risk_minor,
     "Attributed to incidents that are still open. Computed by the detection engine, never by a model."],
    ["Recoverable", r.recoverable_minor,
     "The eligible subset. Eligibility is deterministic — a rule, not a judgement."],
  ];
  const activity: [string, number, string][] = [
    ["Attempted", r.attempted_minor,
     "An action was placed for these. Attempted is not recovered."],
    ["Outstanding", r.outstanding_minor,
     "Attempted and not yet resolved either way."],
  ];
  const settled: [string, number, string][] = [
    ["Recovered", r.recovered_minor,
     "Independently verified at the provider. This is the only number here that asserts money came back."],
    ["Failed", r.failed_minor,
     "Verified as not having taken effect."],
    ["Unknown", r.unknown_minor,
     "Attempted, outcome not established. Being reconciled — see the Action Center."],
  ];

  return (
    <>
      {r.invariants_broken.length > 0 ? (
        <div className="banner danger">
          <strong>These figures do not nest.</strong> {r.invariants_broken.join("; ")}.
          The ledger is rendered anyway: a page that refuses to draw is a page
          nobody can use to find out why.
        </div>
      ) : null}

      <section className="card">
        <h3 className="card-title">Exposure</h3>
        <Figures items={exposure} />
        <h3 className="card-title">In flight</h3>
        <Figures items={activity} />
        <h3 className="card-title">Settled</h3>
        <Figures items={settled} />
        <p className="sub" style={{ marginBottom: 0 }}>{r.basis}</p>
      </section>

      <section className="card">
        <h3 className="card-title">By incident</h3>
        {r.by_incident.length === 0 ? (
          <Empty>
            No open incident carries revenue at risk. Incidents appear here as
            soon as detection attributes an amount to one.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table aria-label="Revenue at risk by incident">
              <thead>
                <tr>
                  <th scope="col">Incident</th><th scope="col">Type</th>
                  <th scope="col">Severity</th><th scope="col">At risk</th>
                  <th scope="col">Recoverable</th><th scope="col">Recovered</th>
                </tr>
              </thead>
              <tbody>
                {r.by_incident.map((i) => (
                  <tr key={i.incident_id}>
                    <td>
                      <Link to={`/incidents/${i.incident_id}`} className="mono">
                        {i.incident_id}
                      </Link>
                      <div className="muted">{i.title}</div>
                    </td>
                    <td>{i.incident_type.replace(/_/g, " ")}</td>
                    <td>{i.severity}</td>
                    <td className="mono"><Money minor={i.revenue_at_risk_minor} /></td>
                    <td className="mono"><Money minor={i.recoverable_minor} /></td>
                    <td className="mono"><Money minor={i.recovered_minor} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h3 className="card-title">By payment method</h3>
        {r.by_method.length === 0 ? (
          <Empty>
            Nothing is attributed to a payment method yet. This fills once a
            recovery plan has candidates.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table aria-label="Revenue at risk by payment method">
              <thead>
                <tr>
                  <th scope="col">Method</th><th scope="col">Recoverable</th>
                  <th scope="col">Recovered</th>
                </tr>
              </thead>
              <tbody>
                {r.by_method.map((m) => (
                  <tr key={m.method}>
                    <td>{m.method}</td>
                    <td className="mono"><Money minor={m.recoverable_minor} /></td>
                    <td className="mono"><Money minor={m.recovered_minor} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function Figures({ items }: { items: [string, number, string][] }) {
  return (
    <div className="tiles">
      {items.map(([label, minor, hint]) => (
        <div className="tile" key={label} title={hint}>
          <span className="tile-n mono"><Money minor={minor} /></span>
          <span className="tile-l">{label}</span>
        </div>
      ))}
    </div>
  );
}
