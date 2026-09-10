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
import type {
  AgentPosture, CommandCenter as CommandCenterData, FunnelStage, ProviderPosture,
} from "../api/types";
import {
  Empty, ErrorBanner, Money, RecoveredSplit, SectionHead, Skeleton, When,
} from "../components/Bits";
import { aiModeSpec } from "../components/AgentActivity";
import { Status, statusSpec } from "../components/Status";
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
          {/* Optional-chained: a server older than these fields omits them,
              and the rest of the page is still worth having. */}
          {d.agent ? <AgentStatus a={d.agent} /> : null}
          {d.provider ? <ProviderStatus p={d.provider} /> : null}
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
        {tiles.map((t) => {
          // The tone comes from the same status vocabulary as everywhere else,
          // but it is carried by the TILE rather than by a pill inside it.
          //
          // The pill used to sit here in full, and on three of four tiles it
          // repeated the label word for word -- "0 / Awaiting approval /
          // [Awaiting approval]". On the fourth it was worse: "Open incidents"
          // carried a pill reading "Running", because RUNNING had been chosen
          // for its colour and then rendered as a claim. A tone is not a label.
          //
          // The glyph stays, because P1-08 requires a state to be legible
          // without colour, and it is `aria-hidden` beside the count and label
          // that a screen reader already reads.
          const spec = statusSpec(t.status);
          const quiet = t.count === 0;
          return (
            <Link key={t.to + t.label}
                  className={`tile ${quiet ? "is-quiet" : `t-${spec.tone}`}`}
                  to={t.to} title={t.hint}>
              <span className="tile-n">{t.count}</span>
              <span className="tile-l">
                <span className="tile-g" aria-hidden="true">{spec.glyph}</span>
                {t.label}
              </span>
            </Link>
          );
        })}
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

      {/* Beside the tile rather than instead of it. "Recovered" keeps meaning
          what it meant; this says what it is made of, because a refund returned
          to a customer and revenue captured back are not the same good news. */}
      <RecoveredSplit captured={r.recovered_captured_minor}
                      refunded={r.recovered_refunded_minor} />

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
  // Zero rupees recedes, for the same reason a queue of zero does: four
  // figures at equal weight make a reader compare four numbers to find the
  // two that are moving. Nothing is hidden -- ₹0.00 is still the answer, and
  // still on screen at full size.
  return (
    <div className={`tile ${minor === 0 ? "is-quiet" : ""}`} title={hint}>
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
  // AT_RISK by name, not `stages[0]`. Every share on this chart is divided by
  // it, and taking the denominator from array position means the whole funnel
  // silently rescales if the server ever emits the stages in another order.
  const top = stages.find((s) => s.stage === "AT_RISK")?.amount_minor ?? 0;

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
            // Clamped. The four figures nest -- at risk >= recoverable >=
            // attempted >= recovered, MerchantOps §49 -- and when they do not,
            // `revenue.invariants_broken` says so in the card above this one.
            // What this chart must not do meanwhile is draw the reassuring
            // version: `.funnel-track` clips at its own width, so a stage
            // larger than AT_RISK rendered as a bar that was exactly full,
            // which is a broken ledger drawn as a complete recovery.
            //
            // Clamped rather than refused: the banner already reports it, and
            // a page that will not render is a page nobody can use to find out
            // why.
            const share = top > 0 ? Math.min(s.amount_minor / top, 1) : 0;
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

/** Which reasoning is configured, and how runs were really produced.
 *
 *  The two can disagree — a model can be configured and every run still be
 *  falling back to the planner — which is why the counts are shown rather than
 *  only the configuration. They are the server's counts, by mode, as sent. */
function AgentStatus({ a }: { a: AgentPosture }) {
  const modes = Object.entries(a.runs_by_mode);
  return (
    <section className="card">
      <h3 className="card-title">Agent status</h3>
      <dl className="kv">
        <dt>Configured reasoning</dt>
        <dd>{a.provider}{a.model ? ` · ${a.model}` : ""}</dd>
        <dt>Fallback</dt>
        <dd>
          {a.fallback_enabled
            ? "On — if the model fails or cannot be reached, the deterministic "
              + "planner runs instead, and the task says so."
            : "Off — a model failure fails the run instead of falling back."}
        </dd>
      </dl>
      <h3 className="card-title" style={{ marginTop: 14 }}>Runs by how they were produced</h3>
      {modes.length === 0 ? (
        <Empty>No run has been recorded for this merchant yet.</Empty>
      ) : (
        <dl className="kv" aria-label="Runs by mode">
          {modes.map(([mode, n]) => (
            <div key={mode} style={{ display: "contents" }}>
              {/* UNRECORDED is the server's key for runs from before the mode
                  was stored — the same "not recorded" the task page shows. */}
              <dt>{aiModeSpec(mode === "UNRECORDED" ? null : mode).label}</dt>
              <dd>{n}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

/** What `adapter_mode` means for money, in words. Never "live": the only
 *  non-mock mode is the provider's TEST environment, and the word "live" on a
 *  payments console is read as real money whatever qualifies it (CONTRACT §7). */
function providerHeadline(mode: string): { title: string; detail: string } {
  if (mode === "mock") {
    return { title: "Mock provider — no real money moves.",
             detail: "Actions are answered by a simulated provider. Nothing is "
                     + "sent to a payment provider." };
  }
  if (mode === "live_test_mode") {
    return { title: "Provider test mode — no real money moves.",
             detail: "Actions reach the provider's test environment with test "
                     + "credentials. Test payments are not real payments." };
  }
  // Rendered as received rather than guessed at: an unfamiliar mode is a
  // reason to check the server, not a reason to reassure.
  return { title: `Adapter mode: ${mode}`,
           detail: "Not a mode this console recognises. Check the server's "
                   + "configuration before relying on it." };
}

function ProviderStatus({ p }: { p: ProviderPosture }) {
  const head = providerHeadline(p.adapter_mode);
  return (
    <section className="card">
      <h3 className="card-title">Provider status</h3>
      <p className={`banner ${p.adapter_mode === "mock" || p.adapter_mode === "live_test_mode"
                               ? "info" : "warn"}`}>
        <strong>{head.title}</strong> {head.detail}
      </p>
      <dl className="kv">
        <dt>Adapter mode</dt><dd>{p.adapter_mode}</dd>
        <dt>Webhook signatures</dt>
        <dd>
          {p.webhook_signature_verification
            ? "Verified — a delivery that fails its signature is refused."
            : "No webhook secret configured — deliveries are recorded but never acted on."}
        </dd>
        <dt>Webhooks pending</dt><dd>{p.webhooks_pending}</dd>
        <dt>Webhooks dead-lettered</dt><dd>{p.webhooks_dead_lettered}</dd>
        <dt>Last webhook</dt>
        <dd>{p.last_webhook_at ? <When iso={p.last_webhook_at} /> : "none received"}</dd>
      </dl>
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
