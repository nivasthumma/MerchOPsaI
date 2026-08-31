import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { IncidentDetail as Detail } from "../api/types";
import {
  Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";

/** MerchantOps §51.
 *
 *  Two things this page is careful about.
 *
 *  Evidence that came from merchant free text is rendered as quarantined data,
 *  visibly. The backend tags it `untrusted` and stripping that flag here would
 *  push the judgement onto whoever reads the page.
 *
 *  Expected recovery never appears without its basis, and never in the same
 *  breath as anything recovered — §49's whole point.
 */
export default function IncidentDetail() {
  const { incidentId = "" } = useParams();
  const [inc, setInc] = useState<Detail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      setInc(await api.getIncident(incidentId));
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }, [incidentId]);

  useEffect(() => { void load(); }, [load]);

  if (error) return <ErrorBanner error={error} />;
  if (!inc) return <Skeleton rows={6} />;

  const plan = inc.recovery;

  return (
    <div className="incident">
      <SectionHead title={inc.title}>
        <span data-sev={inc.severity}>{inc.severity}</span>{" "}
        <span className="muted">{inc.status}</span>
      </SectionHead>

      <StatStrip items={[
        ["Type", inc.type],
        ["Revenue at risk", <Money key="r" minor={inc.revenue_at_risk_minor} />],
        ["Started", <When key="s" iso={inc.started_at} />],
        ["Detected", <When key="d" iso={inc.detected_at} />],
        ["Rule", <code key="k">{inc.detection_rule}</code>],
      ]} />

      <p>{inc.summary}</p>

      <SectionHead title="Evidence" count={inc.evidence.length} />
      {inc.evidence.length === 0 ? <Empty>None recorded.</Empty> : (
        <ul className="evidence" aria-label="Evidence">
          {inc.evidence.map((e) => (
            <li key={e.id} data-untrusted={e.untrusted || undefined}>
              <span className="k">{e.key}</span>
              <span className="v">{String(e.value)}</span>
              <span className="muted src">{e.source}</span>
              {e.untrusted && (
                <span className="tag warn" title="Merchant free text. Data, never instructions.">
                  untrusted
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      <SectionHead title="Recovery" />
      {!plan ? <Empty>No recovery plan yet.</Empty> : (
        <>
          <StatStrip items={[
            ["Intervention", plan.intervention],
            ["Plan status", plan.status],
            ["Recoverable", <Money key="e" minor={plan.eligible_recovery_minor} />],
            ["Expected", <Money key="x" minor={plan.expected_recovery_minor} />],
          ]} />
          {/* The estimate never travels without the reasoning that produced it. */}
          <p className="muted basis">{plan.expected_recovery_basis}</p>
          {plan.stop_rule && (
            <div className="banner warn" role="status">
              <strong>Stopped: {plan.stop_rule}.</strong> {plan.stop_reason}
            </div>
          )}
          <StatStrip items={[
            ["Max recovery", <Money key="b" minor={plan.budget.max_recovery_minor} />],
            ["Max actions", plan.budget.max_actions],
            ["Max per customer", plan.budget.max_attempts_per_customer],
          ]} />
        </>
      )}

      <SectionHead title="Investigations" count={inc.tasks.length} />
      {inc.tasks.length === 0 ? <Empty>Not investigated yet.</Empty> : (
        <ul className="tasks" aria-label="Investigations">
          {inc.tasks.map((t) => (
            <li key={t.id}>
              <Link to={`/tasks/${t.id}`}>{t.id}</Link>{" "}
              <span className="muted">{t.status} · {t.tool_calls} tool calls</span>
              {t.final_answer && <p>{t.final_answer}</p>}
            </li>
          ))}
        </ul>
      )}

      <SectionHead title="Timeline" count={inc.timeline.length} />
      {/* Read from the audit trail, so it reports what the application did
          rather than a narrative assembled beside it. */}
      <ol className="timeline" aria-label="Timeline">
        {inc.timeline.map((e, i) => (
          <li key={`${e.at}-${i}`}>
            <When iso={e.at} />
            <span className="ev">{e.event.replace(/_/g, " ")}</span>
            {e.detail.from != null && e.detail.to != null ? (
              <span className="muted">{String(e.detail.from)} → {String(e.detail.to)}</span>
            ) : null}
            {e.task_id && <Link to={`/tasks/${e.task_id}`}>{e.task_id}</Link>}
          </li>
        ))}
      </ol>
    </div>
  );
}
