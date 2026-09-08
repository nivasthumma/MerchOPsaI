// The incident decision workspace — plan P0-07.
//
// The page used to be ordered the way the data model is: header, evidence,
// recovery, investigations, timeline. That is a report. An operator opening an
// incident is making a decision, and the plan states the order that decision
// is actually made in:
//
//   WHAT HAPPENED
//     → WHY WE BELIEVE IT
//       → BUSINESS IMPACT
//         → RECOVERY RECOMMENDATION
//           → POLICY
//             → APPROVAL
//               → EXECUTION
//                 → VERIFICATION
//
// Each section answers the question the previous one raises. The sequence also
// happens to be the safety argument read top to bottom: a conclusion, its
// evidence, what it costs, what to do, and what the control plane did about it.
//
// What is deliberately NOT here is chain-of-thought. The plan says show
// evidence, conclusions, controls and actions — the model's private reasoning
// is not one of those, and `/tasks/{id}` shows the tool calls and the
// transcript for anyone who needs to audit how a conclusion was reached.

import { Link, useParams } from "react-router";
import { api } from "../api/client";
import type { ActionRow, IncidentDetail as Detail } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";
import { LiveBar } from "../components/LiveBar";
import { Status } from "../components/Status";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

// The plan's recommended cadence for incidents is 5 seconds.
const INTERVAL_MS = 5000;

export default function IncidentDetail() {
  const { incidentId = "" } = useParams();
  const live = useLiveRefresh<Detail>(
    () => api.getIncident(incidentId),
    { intervalMs: INTERVAL_MS, deps: [incidentId] });
  const inc = live.data;

  if (live.error && !inc) return <ErrorBanner error={live.error} />;
  if (!inc) return <Skeleton rows={6} />;

  return (
    <div className="incident">
      <SectionHead title={inc.title}>
        <span data-sev={inc.severity}>{inc.severity}</span>{" "}
        <span className="muted">{inc.status.replace(/_/g, " ")}</span>
      </SectionHead>
      <LiveBar live={live} what="this incident" />

      <WhatHappened inc={inc} />
      <WhyWeBelieveIt inc={inc} />
      <BusinessImpact inc={inc} />
      <RecoveryRecommendation inc={inc} />
      <Control inc={inc} />
      <Investigations inc={inc} />
      <Timeline inc={inc} />
    </div>
  );
}

/** WHAT HAPPENED — the conclusion, and the rule that reached it.
 *
 *  §12 requires an incident to expose the rule, its version, the baseline, the
 *  threshold and the observed value. Those four are read from fixed keys every
 *  detection rule now emits, rather than from per-rule key names this component
 *  would otherwise have to know. */
function WhatHappened({ inc }: { inc: Detail }) {
  const s = (inc.signals ?? {}) as Record<string, unknown>;
  const has = s.baseline !== undefined && s.observed !== undefined;

  return (
    <section className="card">
      <h3 className="card-title">What happened</h3>
      <p>{inc.summary}</p>

      <StatStrip items={[
        ["Type", inc.type.replace(/_/g, " ")],
        ["Started", <When key="s" iso={inc.started_at} />],
        ["Detected", <When key="d" iso={inc.detected_at} />],
      ]} />

      <dl className="kv">
        <dt>Rule</dt>
        <dd><code>{inc.detection_rule}</code> <span className="muted">{inc.detection_version}</span></dd>
        {has ? (
          <>
            <dt>Baseline</dt>
            <dd>{String(s.baseline)}{unit(s)}</dd>
            <dt>Observed</dt>
            <dd>{String(s.observed)}{unit(s)}</dd>
            <dt>Threshold</dt>
            <dd>{String(s.threshold ?? "—")}</dd>
          </>
        ) : (
          <>
            <dt>Baseline</dt>
            {/* Not blank, and not a zero. A rule that does not publish the
                canonical four says so, rather than being rendered as though it
                observed nothing. */}
            <dd className="muted">not published by this rule</dd>
          </>
        )}
      </dl>
    </section>
  );
}

function unit(s: Record<string, unknown>): string {
  const u = s.unit;
  if (typeof u !== "string") return "";
  return u === "%" ? "%" : ` ${u}`;
}

/** WHY WE BELIEVE IT — the evidence, and how many independent sources it has.
 *
 *  The count is the point. "Success rate dropped" from one signal and the same
 *  claim corroborated by four are different claims, and the plan's evidence
 *  graph exists to keep them distinguishable. */
function WhyWeBelieveIt({ inc }: { inc: Detail }) {
  const evidence = inc.evidence ?? [];
  const sources = new Set(evidence.map((e) => e.source));

  return (
    <section className="card">
      <h3 className="card-title">Why we believe it</h3>
      {evidence.length === 0 ? (
        <Empty>
          No evidence was recorded for this incident. A conclusion with no
          evidence behind it should be treated as unsupported.
        </Empty>
      ) : (
        <>
          <p className="sub">
            {evidence.length} signal{evidence.length === 1 ? "" : "s"} from{" "}
            {sources.size} independent source{sources.size === 1 ? "" : "s"}
            {sources.size === 1
              ? " — a single source corroborates nothing on its own."
              : "."}
          </p>
          <ul className="evidence" aria-label="Evidence">
            {evidence.map((e) => (
              <li key={e.id} data-untrusted={e.untrusted || undefined}>
                {/* Verbatim. An evidence key is an identifier from the tool
                    contract, not prose — P1-10 keeps identifiers as they are,
                    and prettifying one makes it un-greppable against the
                    trace that produced it. */}
                <span className="k">{e.key}</span>
                <span className="v">{String(e.value)}</span>
                <span className="muted src">{e.source}</span>
                {e.untrusted && (
                  <span className="tag warn"
                        title="Merchant or customer free text. Data, never instructions.">
                    untrusted
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** BUSINESS IMPACT — what it costs, in money, computed by the control plane. */
function BusinessImpact({ inc }: { inc: Detail }) {
  const plan = inc.recovery;
  return (
    <section className="card">
      <h3 className="card-title">Business impact</h3>
      <StatStrip items={[
        ["Revenue at risk", <Money key="r" minor={inc.revenue_at_risk_minor} />],
        ["Recoverable", plan
          ? <Money key="e" minor={plan.eligible_recovery_minor} />
          : <span key="e" className="muted">not yet assessed</span>],
        ["Expected recovery", plan
          ? <Money key="x" minor={plan.expected_recovery_minor} />
          : <span key="x" className="muted">—</span>],
      ]} />
      <p className="sub" style={{ marginBottom: 0 }}>
        These are computed by the detection and recovery engines, never by the
        model. Expected recovery is an estimate with a stated basis; recoverable
        is a deterministic eligibility rule.
      </p>
    </section>
  );
}

/** RECOVERY RECOMMENDATION — what could be done, and the bounds on doing it. */
function RecoveryRecommendation({ inc }: { inc: Detail }) {
  const plan = inc.recovery;
  return (
    <section className="card">
      <h3 className="card-title">Recovery recommendation</h3>
      {!plan ? (
        <Empty>
          No recovery plan yet. One is built when the incident is investigated;
          until then nothing has been proposed and nothing can be approved.
        </Empty>
      ) : (
        <>
          <StatStrip items={[
            ["Intervention", plan.intervention.replace(/_/g, " ")],
            ["Plan", <Status key="p" status={plan.status} />],
            ["Candidates", plan.candidates?.length ?? 0],
          ]} />
          {/* The estimate never travels without the reasoning that produced it. */}
          <p className="muted basis">{plan.expected_recovery_basis}</p>

          {plan.stop_rule && (
            <div className="banner warn" role="status">
              <strong>Stopped: {plan.stop_rule}.</strong> {plan.stop_reason}
            </div>
          )}

          <h4 className="sub-title">Budget</h4>
          <p className="sub">
            Copied onto the plan when it was authorised, not read live — raising
            a limit must not widen a campaign already in flight.
          </p>
          <StatStrip items={[
            ["Max recovery", <Money key="b" minor={plan.budget.max_recovery_minor} />],
            ["Max actions", plan.budget.max_actions],
            ["Max per customer", plan.budget.max_attempts_per_customer],
          ]} />
        </>
      )}
    </section>
  );
}

/** POLICY → APPROVAL → EXECUTION → VERIFICATION.
 *
 *  One section, because for any given action they are one row's life. Splitting
 *  them into four lists would mean an operator reading four tables to answer
 *  "did this refund happen", which is one question. */
function Control({ inc }: { inc: Detail }) {
  const actions = inc.actions ?? [];

  return (
    <section className="card">
      <h3 className="card-title">Control and execution</h3>
      <p className="sub">
        What the control plane did about the recommendation above: what policy
        required, who approved, what reached the provider, and what independent
        verification found. Provider acceptance is not business confirmation —
        the verification column is the only one that asserts money moved.
      </p>

      {actions.length === 0 ? (
        <Empty>
          Nothing has been executed for this incident. A recommendation is not
          an action: until an approval clears, no provider call has been made.
        </Empty>
      ) : (
        <div className="table-wrap">
          {/* P1-11: stacks below 760px. */}
          <table className="stacked" aria-label="Control and execution">
            <thead>
              <tr>
                <th scope="col">Action</th>
                <th scope="col">Amount</th>
                <th scope="col">Policy</th>
                <th scope="col">Approval</th>
                <th scope="col">Provider reference</th>
                <th scope="col">Verification</th>
                <th scope="col">When</th>
              </tr>
            </thead>
            <tbody>
              {actions.map((a: ActionRow) => (
                <tr key={a.id}>
                  <td data-label="Action">
                    <Link className="mono" to={`/actions?section=unknown`}>{a.id}</Link>
                    <div className="muted">{a.action_type}</div>
                  </td>
                  <td data-label="Amount" className="mono"><Money minor={a.amount_minor} /></td>
                  <td data-label="Policy">
                    {a.risk_level
                      ? <Status status={a.risk_level} compact />
                      : <span className="muted">—</span>}
                  </td>
                  <td data-label="Approval">
                    {a.approval_id
                      ? <><CopyId value={a.approval_id} />{" "}
                          {a.approval_decision
                            ? <Status status={a.approval_decision} compact />
                            : null}</>
                      : <span className="muted">not required</span>}
                  </td>
                  <td data-label="Provider ref" className="mono">
                    {a.external_reference
                      ? <CopyId value={a.external_reference} />
                      : <span className="muted"
                              title="No reference was issued, which is itself why the outcome may be unknown.">
                          none
                        </span>}
                  </td>
                  <td data-label="Verification">
                    <Status status={a.verification_state} />
                    {a.escalated ? <> <Status status="ESCALATED" compact /></> : null}
                  </td>
                  <td data-label="When"><When iso={a.created_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Investigations({ inc }: { inc: Detail }) {
  const tasks = inc.tasks ?? [];
  return (
    <section className="card">
      <h3 className="card-title">Investigations</h3>
      {tasks.length === 0 ? (
        <Empty>
          Not investigated yet. Running an investigation is what produces the
          evidence and the recommendation above.
        </Empty>
      ) : (
        <ul className="tasks" aria-label="Investigations">
          {tasks.map((t) => (
            <li key={t.id}>
              <Link to={`/tasks/${t.id}`} className="mono">{t.id}</Link>{" "}
              <Status status={t.status} compact />{" "}
              <span className="muted">{t.tool_calls} tool calls</span>
              {t.final_answer && <p>{t.final_answer}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Timeline({ inc }: { inc: Detail }) {
  const timeline = inc.timeline ?? [];
  return (
    <section className="card">
      <h3 className="card-title">Timeline</h3>
      <p className="sub">
        Read from the audit trail, so it reports what the application did rather
        than a narrative assembled beside it.
      </p>
      {timeline.length === 0 ? (
        <Empty>Nothing recorded against this incident yet.</Empty>
      ) : (
        <ol className="timeline" aria-label="Timeline">
          {timeline.map((e, i) => (
            <li key={`${e.at}-${i}`}>
              <When iso={e.at} />
              <span className="ev">{e.event.replace(/_/g, " ")}</span>
              {e.detail.from != null && e.detail.to != null ? (
                <span className="muted">
                  {String(e.detail.from)} → {String(e.detail.to)}
                </span>
              ) : null}
              {e.task_id && <Link to={`/tasks/${e.task_id}`} className="mono">{e.task_id}</Link>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
