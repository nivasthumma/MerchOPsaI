import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Approval, ReplayResult, Task, TraceEvent } from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip,
  StatusPill, VerificationPill, When,
} from "../components/Bits";
import { groupOf, iconOf, summarise, type TraceGroup } from "./trace-summary";

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  const [task, setTask] = useState<Task | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [replay, setReplay] = useState<{ mode: string; result: ReplayResult } | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, tr] = await Promise.all([api.getTask(taskId), api.getTrace(taskId)]);
      setTask(t);
      setTrace(tr.trace);
    } catch (e) {
      setError(e as ApiError);
    }
  }, [taskId]);

  useEffect(() => { void load(); }, [load]);

  async function act(name: string, fn: () => Promise<unknown>) {
    setBusy(name);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(null);
    }
  }

  if (error && !task) return <ErrorBanner error={error} />;
  if (!task) {
    return (
      <div className="card">
        <Skeleton rows={4} />
      </div>
    );
  }

  const pending = task.approvals.find((a) => a.decision === "PENDING");
  const decided = task.approvals.filter((a) => a.decision !== "PENDING");
  const unsettled = task.actions.filter(
    (a) => a.verification_state === "UNKNOWN" || a.verification_state === "PARTIAL");

  return (
    <>
      <div className="page-head">
        <div className="crumb"><Link to="/">← Investigate</Link></div>
        <h1>
          <CopyId value={task.id} label="task id" />
          <StatusPill status={task.status} />
          {task.is_replay ? <span className="pill neutral">replay</span> : null}
        </h1>
        <p className="request">{task.request}</p>
      </div>

      <StatStrip items={[
        ["Tool calls", task.tool_calls ?? 0],
        ["LLM turns", task.llm_turns ?? 0],
        ["Duration", `${task.duration_ms ?? 0} ms`],
        ["Reasoning", task.model_version],
        ["Prompt", task.prompt_version],
      ]} />

      {/* Announced rather than only shown: an operator acting on a refund needs
          to hear a refusal even when focus is elsewhere on the page. */}
      <div role="alert" aria-live="assertive">
        <ErrorBanner error={error} />
      </div>

      {pending ? (
        <ApprovalGate
          approval={pending} busy={busy}
          onApprove={() => act("approve", () => api.approve(task.id))}
          onReject={() => act("reject", () => api.reject(task.id))}
        />
      ) : null}

      {task.final_answer ? (
        <div className="card">
          <SectionHead title="Answer" />
          <pre>{task.final_answer}</pre>
        </div>
      ) : null}

      {task.actions.length ? (
        <div className="card">
          <SectionHead title="Actions and verification" count={`${task.actions.length}`} />
          <p className="sub">
            Verification reads the payment back from the provider. It never trusts the
            response to the request that created it.
          </p>

          {task.actions.map((a) => (
            <div className="action-card" key={a.id}>
              <div className="head">
                <strong>{a.action_type}</strong>
                <span className="pill neutral">{a.status}</span>
                <VerificationPill state={a.verification_state} />
                <span className="spacer" style={{ marginLeft: "auto" }}>
                  <Money minor={a.amount_minor} />
                </span>
              </div>
              <dl className="kv" style={{ marginTop: 10 }}>
                <dt>Payment</dt><dd>{a.target_payment_id ?? "—"}</dd>
                <dt>External</dt>
                <dd>{a.external_payment_id ?? "—"}</dd>
                <dt>Reference</dt>
                <dd>{a.external_reference
                  ? <CopyId value={a.external_reference} label="external reference" />
                  : <span className="muted">none received</span>}</dd>
                {a.verify_attempts > 0 ? (
                  <><dt>Verify attempts</dt><dd>{a.verify_attempts}</dd></>
                ) : null}
              </dl>
              {a.verification_detail ? (
                <>
                  <p className="reason">{a.verification_detail.reason}</p>
                  {a.verification_detail.expected || a.verification_detail.actual ? (
                    <details>
                      <summary>expected vs actual</summary>
                      <pre>{JSON.stringify(
                        { expected: a.verification_detail.expected,
                          actual: a.verification_detail.actual }, null, 2)}</pre>
                    </details>
                  ) : null}
                </>
              ) : null}
            </div>
          ))}

          {unsettled.length ? (
            <div className="banner unknown">
              <strong>{unsettled.length} action{unsettled.length > 1 ? "s" : ""} unsettled.</strong>{" "}
              UNKNOWN is an honest answer, not a failure: the provider may or may not have
              applied it. Re-verification re-reads state by idempotency key and never
              re-issues the action.
              <div className="row" style={{ marginTop: 10 }}>
                <button disabled={!!busy} aria-busy={busy === "reverify"}
                        onClick={() => act("reverify", () => api.reverify(task.id))}>
                  {busy === "reverify" ? "Re-reading…" : "Re-verify"}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {decided.length ? (
        <div className="card">
          <SectionHead title="Approval history" count={`${decided.length}`} />
          <div className="table-wrap">
            <table>
              <caption className="sub" style={{ textAlign: "left", captionSide: "top" }}>
                Who decided, and on what. Kept after the fact — an approval that leaves no
                record is not an approval.
              </caption>
              <thead>
                <tr><th>Approval</th><th>Action</th><th>Risk</th><th>Decision</th>
                    <th>Decided by</th><th>Expired</th></tr>
              </thead>
              <tbody>
                {decided.map((a) => (
                  <tr key={a.id}>
                    <td className="mono">{a.id}</td>
                    <td>{a.action_type}</td>
                    <td><span className="pill warn">{a.risk_level}</span></td>
                    <td>
                      <span className={`pill ${a.decision === "APPROVED" ? "ok" : "danger"}`}>
                        {a.decision}
                      </span>
                    </td>
                    <td className="mono">{a.decided_by ?? "—"}</td>
                    <td className="muted"><When iso={a.expires_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      <div className="card">
        <SectionHead title="Replay" />
        <p className="sub">
          Both modes run against frozen tool results and must produce zero external calls.
          That is asserted, not assumed.
        </p>
        <div className="row">
          <button disabled={!!busy} aria-busy={busy === "PLAYBACK"}
                  onClick={() => act("PLAYBACK", async () =>
                    setReplay({ mode: "PLAYBACK", result: await api.replay(task.id, "PLAYBACK") }))}>
            PLAYBACK
          </button>
          <button disabled={!!busy} aria-busy={busy === "RE_REASON"}
                  onClick={() => act("RE_REASON", async () =>
                    setReplay({ mode: "RE_REASON", result: await api.replay(task.id, "RE_REASON") }))}>
            RE_REASON
          </button>
          {busy === "PLAYBACK" || busy === "RE_REASON" ? <Busy /> : null}
        </div>
        {replay ? <ReplayPanel mode={replay.mode} result={replay.result} /> : null}
      </div>

      <TracePanel events={trace} />
    </>
  );
}

function ApprovalGate(
  { approval, busy, onApprove, onReject }:
  { approval: Approval; busy: string | null; onApprove: () => void; onReject: () => void },
) {
  const expires = new Date(approval.expires_at);
  const expired = expires.getTime() < Date.now();
  return (
    <div className="card cta">
      <SectionHead title="Approval required">
        <span className="pill warn">{approval.risk_level} risk</span>
      </SectionHead>
      <p className="sub">
        The policy engine stopped execution. <strong>No external call has been made.</strong>{" "}
        Authorization is re-checked server-side on approval — this button is a request, not
        a decision.
      </p>
      <dl className="kv">
        <dt>Action</dt><dd>{approval.action_type}</dd>
        <dt>Payment</dt><dd>{String(approval.action_payload.synthetic_payment_id ?? "—")}</dd>
        <dt>Amount</dt>
        <dd><Money minor={approval.action_payload.amount_minor as number | undefined} /></dd>
        <dt>Expires</dt>
        <dd>
          <When iso={approval.expires_at} />
          {expired ? <span className="pill danger" style={{ marginLeft: 8 }}>expired</span> : null}
        </dd>
      </dl>
      {typeof approval.action_payload.reason === "string" ? (
        <p style={{ marginTop: 12 }}>{approval.action_payload.reason}</p>
      ) : null}
      {expired ? (
        <div className="banner warn" style={{ marginTop: 12 }}>
          This approval has passed its expiry. The server will refuse it — the button is
          left enabled so the refusal comes from the authority, not from this page.
        </div>
      ) : null}
      <div className="row" style={{ marginTop: 16 }}>
        <button className="primary" disabled={!!busy} aria-busy={busy === "approve"}
                onClick={onApprove}>
          {busy === "approve" ? "Executing…" : "Approve and execute"}
        </button>
        <button className="danger" disabled={!!busy} onClick={onReject}>Reject</button>
      </div>
    </div>
  );
}

function ReplayPanel({ mode, result }: { mode: string; result: ReplayResult }) {
  const clean = result.external_calls === 0;
  return (
    <>
      <div className={`banner ${clean ? "info" : "danger"}`} style={{ marginTop: 14 }}>
        <strong>{mode}: {result.external_calls} external calls.</strong>{" "}
        {clean
          ? "No financial side effect, as required."
          : "This is a defect — replay must never move money."}
        {result.reasoning_diverged !== undefined
          ? ` Reasoning diverged: ${result.reasoning_diverged}${
              result.divergence_kind ? ` (${result.divergence_kind})` : ""}.`
          : ""}
      </div>
      {result.steps?.length ? (
        <p className="sub" style={{ marginTop: 10 }}>
          Steps: <span className="mono">{result.steps.join(" → ")}</span>
        </p>
      ) : null}
      {result.note ? <p className="sub">{result.note}</p> : null}
      <details>
        <summary>raw result</summary>
        <pre>{JSON.stringify(result, null, 2)}</pre>
      </details>
    </>
  );
}

const FILTERS: { key: TraceGroup | "all"; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "policy", label: "Policy" },
  { key: "approval", label: "Approval" },
  { key: "action", label: "Action" },
  { key: "verification", label: "Verification" },
];

function TracePanel({ events }: { events: TraceEvent[] }) {
  const [filter, setFilter] = useState<TraceGroup | "all">("all");
  const shown = useMemo(
    () => events.filter((e) => filter === "all" || groupOf(e.event) === filter),
    [events, filter]);

  return (
    <div className="card">
      <SectionHead title="Audit trace" count={`${shown.length} of ${events.length}`} />
      <p className="sub">
        Append-only, enforced by PostgreSQL triggers. Secrets are redacted before write.
        This is the primary record of what the system did.
      </p>
      <div className="filters" role="group" aria-label="Filter trace by stage">
        {FILTERS.map((f) => (
          <button key={f.key} aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
            {f.label}
          </button>
        ))}
      </div>
      {shown.length === 0 ? <Empty>No events at this stage.</Empty> : (
        <ul className="trace">
          {shown.map((e) => {
            const line = summarise(e);
            return (
              <li key={e.id} className={`g-${groupOf(e.event)}`}>
                <span className="when">{new Date(e.at).toLocaleTimeString()}</span>
                <span className="icon" aria-hidden="true">{iconOf(e.event)}</span>
                <span className="what">
                  {e.event}
                  {line ? <div className="summary-line">{line}</div> : null}
                  {Object.keys(e.payload ?? {}).length ? (
                    <details>
                      <summary>payload</summary>
                      <pre>{JSON.stringify(e.payload, null, 2)}</pre>
                    </details>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
