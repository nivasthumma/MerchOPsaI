// The home screen — plan P0-05.
//
// The question it answers is "what needs my attention?", and it has to answer
// it before the operator reads anything. So the attention row is first, above
// the money: a merchant who opens this page during an incident is not looking
// for a revenue summary, they are looking for the thing that is waiting on
// them.
//
// Every number here comes from `/command-center` in one read. Nothing on this
// page is computed in the browser — a figure this client added up would be a
// figure nobody can audit, and the four fetches it would take to assemble it
// would each be from a different instant.

import { Link } from "react-router";
import { api } from "../api/client";
import type { CommandCenter as CommandCenterData, FunnelStage } from "../api/types";
import { Empty, ErrorBanner, Money, SectionHead, Skeleton } from "../components/Bits";
import { Status } from "../components/Status";
import { LiveBar } from "../components/LiveBar";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

// The plan's recommended cadence for this screen is 5–10 seconds.
const INTERVAL_MS = 8000;

export default function CommandCenter() {
  const live = useLiveRefresh<CommandCenterData>(
    () => api.commandCenter(), { intervalMs: INTERVAL_MS });
  const d = live.data;

  return (
    <>
      <SectionHead title="Command Center">
        What needs your attention, and what the money is doing.
      </SectionHead>
      <LiveBar live={live} />
      {live.error && !d ? <ErrorBanner error={live.error} /> : null}

      {!d ? <Skeleton rows={6} /> : (
        <>
          <Attention d={d} />
          <RevenueHealth d={d} />
          <Funnel stages={d.funnel} />
          <Activity d={d} />
        </>
      )}
    </>
  );
}

/** The work waiting on a person. First on the page, deliberately.
 *
 *  Each tile links to the queue that resolves it. A count with nowhere to go is
 *  a notification, and the plan is explicit that this is a control plane rather
 *  than a dashboard. */
function Attention({ d }: { d: CommandCenterData }) {
  const a = d.attention;
  const tiles: {
    to: string; label: string; count: number; status: string; hint: string;
  }[] = [
    { to: "/actions?section=awaiting_approval", label: "Awaiting approval",
      count: a.approvals_pending, status: "AWAITING_APPROVAL",
      hint: "Policy requires a person. Nothing has been sent to the provider." },
    { to: "/actions?section=unknown", label: "Unknown", count: a.unknown_actions,
      status: "UNKNOWN",
      hint: "Unresolved financial work. Being reconciled automatically." },
    { to: "/actions?section=escalated", label: "Escalated",
      count: a.escalated_actions, status: "ESCALATED",
      hint: "Automatic reconciliation is exhausted. A person owns these." },
    { to: "/incidents", label: "Open incidents", count: a.open_incidents,
      status: "RUNNING", hint: "Detected problems that are not yet resolved." },
  ];

  const expired = a.approvals_expired;

  return (
    <section className="card">
      <h3 className="card-title">Needs attention</h3>
      <div className="tiles">
        {tiles.map((t) => (
          <Link key={t.to + t.label} className="tile" to={t.to} title={t.hint}>
            <span className="tile-n">{t.count}</span>
            <span className="tile-l">{t.label}</span>
            <Status status={t.status} compact />
          </Link>
        ))}
      </div>

      {/* Not a tile. An expired approval is not work waiting on someone — it is
          work that can no longer be done, and putting it beside the live
          queues would invite an operator to try. */}
      {expired > 0 ? (
        <p className="sub" style={{ marginBottom: 0 }}>
          <Status status="EXPIRED" />{" "}
          {expired} approval{expired === 1 ? "" : "s"} passed their window and
          cannot execute. A new decision has to be requested.
        </p>
      ) : null}

      {a.critical_incidents > 0 ? (
        <p className="sub" style={{ marginBottom: 0 }}>
          <strong>{a.critical_incidents}</strong> of the open incidents{" "}
          {a.critical_incidents === 1 ? "is" : "are"} CRITICAL.
        </p>
      ) : null}
    </section>
  );
}

function RevenueHealth({ d }: { d: CommandCenterData }) {
  const r = d.revenue;
  return (
    <section className="card">
      <h3 className="card-title">Revenue health</h3>
      <div className="tiles">
        <Figure label="At risk" minor={r.at_risk_minor}
                hint="Attributed to incidents that are still open." />
        <Figure label="Recoverable" minor={r.recoverable_minor}
                hint="The eligible subset. Eligibility is deterministic, not a model's judgement." />
        <Figure label="Recovered" minor={r.recovered_minor}
                hint="Independently verified at the provider. Not an estimate." />
        <Figure label="Unresolved" minor={r.unknown_minor}
                hint="Attempted, outcome not yet established." />
      </div>

      {/* Reported rather than hidden. A violated ordering is a reporting defect
          that has to be visible, and a page that refuses to render is a page
          nobody can use to find out why. */}
      {r.invariants_broken.length > 0 ? (
        <p className="banner danger" style={{ marginBottom: 0 }}>
          <strong>These figures do not nest.</strong>{" "}
          {r.invariants_broken.join("; ")}. Treat them as suspect and open the
          ledger.
        </p>
      ) : null}
    </section>
  );
}

function Figure({ label, minor, hint }:
                { label: string; minor: number; hint: string }) {
  return (
    <div className="tile" title={hint}>
      <span className="tile-n mono"><Money minor={minor} /></span>
      <span className="tile-l">{label}</span>
    </div>
  );
}

/** The recovery funnel — P1-03.
 *
 *  Order comes from the server. The bar widths are proportions of the first
 *  stage, so a later stage can never draw wider than an earlier one — the rule
 *  the plan states as "never imply at-risk equals recovered", enforced by the
 *  geometry rather than by remembering it. */
function Funnel({ stages }: { stages: FunnelStage[] }) {
  const top = stages[0]?.amount_minor ?? 0;

  return (
    <section className="card">
      <h3 className="card-title">Recovery funnel</h3>
      {top === 0 ? (
        <Empty>
          Nothing is at risk right now, so there is no funnel to draw. Run
          detection from <Link to="/incidents">Incidents</Link> if you expect
          otherwise.
        </Empty>
      ) : (
        <ol className="funnel">
          {stages.map((s) => {
            const share = top > 0 ? s.amount_minor / top : 0;
            return (
              <li key={s.stage}>
                <div className="funnel-head">
                  <span className="funnel-label">{s.label}</span>
                  <span className="mono"><Money minor={s.amount_minor} /></span>
                </div>
                <div className="funnel-track">
                  <div className="funnel-fill"
                       style={{ width: `${Math.max(share * 100, s.amount_minor > 0 ? 1 : 0)}%` }}
                       role="img"
                       aria-label={`${s.label}: ${(share * 100).toFixed(1)}% of at risk`} />
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function Activity({ d }: { d: CommandCenterData }) {
  return (
    <section className="card">
      <h3 className="card-title">Live activity</h3>
      {d.activity.length === 0 ? (
        <Empty>
          Nothing has happened for this merchant yet. This feed is the audit
          trail, not a simulation — it stays empty until something real does.
        </Empty>
      ) : (
        <ul className="feed">
          {d.activity.map((e, i) => (
            <li key={`${e.created_at}-${i}`}>
              <span className="mono feed-when">
                {new Date(e.created_at).toLocaleTimeString()}
              </span>
              <span className="feed-what">{e.event_type.replace(/_/g, " ")}</span>
              {e.incident_id ? (
                <Link to={`/incidents/${e.incident_id}`} className="mono">{e.incident_id}</Link>
              ) : e.task_id ? (
                <Link to={`/tasks/${e.task_id}`} className="mono">{e.task_id}</Link>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
