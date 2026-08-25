import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type {
  Approval, PlaybackResult, ReplayResult, ReReasonResult, Task, TraceEvent,
} from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip,
  StatusPill, VerificationPill, When,
} from "../components/Bits";
import { Stepper } from "../components/Stepper";
import { useToast } from "../components/Toast";
import { groupOf, iconOf, summarise, type TraceGroup } from "./trace-summary";

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  const [task, setTask] = useState<Task | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [replay, setReplay] = useState<{ mode: string; result: ReplayResult } | null>(null);
  const toast = useToast();

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

  async function act<T>(
    name: string, fn: () => Promise<T>, announce?: (r: T) => [string, string],
  ) {
    setBusy(name);
    setError(null);
    try {
      const result = await fn();
      if (announce) {
        const [title, body] = announce(result);
        toast({ tone: "ok", title, body });
      }
      await load();
    } catch (e) {
      const err = e as ApiError;
      setError(err);
      // A refusal is announced as well as written into the page. It does not
      // auto-dismiss — see ToastHost.
      toast({ tone: err.isConflict ? "warn" : "danger",
              title: err.isConflict ? "Refused by the server" : "Request failed",
              body: `${err.code ? `${err.code} — ` : ""}${err.message}` });
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

      <Stepper task={task} />

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
          onApprove={() => act("approve", () => api.approve(task.id), (t2) => {
            const v = t2.actions[t2.actions.length - 1]?.verification_state;
            return ["Approved and executed",
                    v ? `Independent verification: ${v}` : "Verification pending"];
          })}
          onReject={() => act("reject", () => api.reject(task.id),
                              () => ["Rejected", "No external call was made."])}
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
                        onClick={() => act("reverify", () => api.reverify(task.id),
                          (r) => ["Re-read external state",
                                  `Now ${String((r.verification as { state?: string }).state ?? "unchanged")} — state was read, not re-issued.`])}>
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
                  onClick={() => act("PLAYBACK",
                    () => api.replay(task.id, "PLAYBACK"),
                    (r) => { setReplay({ mode: "PLAYBACK", result: r });
                             return ["Replayed (PLAYBACK)", `${r.external_calls_made} external calls`]; })}>
            PLAYBACK
          </button>
          <button disabled={!!busy} aria-busy={busy === "RE_REASON"}
                  onClick={() => act("RE_REASON",
                    () => api.replay(task.id, "RE_REASON"),
                    (r) => { setReplay({ mode: "RE_REASON", result: r });
                             return ["Replayed (RE_REASON)", `${r.external_calls_made} external calls`]; })}>
            RE_REASON
          </button>
          {busy === "PLAYBACK" || busy === "RE_REASON" ? <Busy /> : null}
        </div>
        {replay ? <ReplayPanel result={replay.result} /> : null}
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

function ReplayPanel({ result }: { result: ReplayResult }) {
  // Both modes report the count in `external_calls_made`. Nothing else on this
  // page matters more: a replay that moved money is a defect, and a replay that
  // did not must not be reported as one.
  const calls = result.external_calls_made;
  const clean = calls === 0;

  return (
    <>
      <div className={`banner ${clean ? "info" : "danger"}`} style={{ marginTop: 16 }}>
        <strong>{result.mode}: {calls} external call{calls === 1 ? "" : "s"}.</strong>{" "}
        {clean
          ? "No financial side effect, as required."
          : "This is a defect — replay must never move money."}
      </div>

      {result.mode === "RE_REASON" ? <ReReasonDetail result={result} /> : null}
      {result.mode === "PLAYBACK" ? <PlaybackDetail result={result} /> : null}

      {result.note ? <p className="sub" style={{ marginTop: 12 }}>{result.note}</p> : null}
      <details>
        <summary>raw result</summary>
        <pre>{JSON.stringify(result, null, 2)}</pre>
      </details>
    </>
  );
}

function PlaybackDetail({ result }: { result: PlaybackResult }) {
  return (
    <>
      <h3>Steps replayed against frozen tool results</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>#</th><th>Tool</th><th>Risk</th><th>Policy</th><th>Result</th><th>Time</th></tr>
          </thead>
          <tbody>
            {result.steps.map((s) => (
              <tr key={s.seq}>
                <td className="mono">{s.seq}</td>
                <td className="mono">{s.tool}</td>
                <td>
                  <span className={`pill ${s.risk_level === "HIGH" ? "warn" : "neutral"}`}>
                    {s.risk_level}
                  </span>
                </td>
                <td>
                  <span className={`pill ${s.policy_decision === "ALLOW" ? "ok" : "warn"}`}>
                    {s.policy_decision}
                  </span>
                </td>
                <td>
                  {s.success ? <span className="pill ok">ok</span>
                             : <span className="pill danger">{s.error_code ?? "failed"}</span>}
                </td>
                <td className="mono muted">{s.duration_ms} ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ReReasonDetail({ result }: { result: ReReasonResult }) {
  const same = result.original_tool_sequence.join("|") === result.replay_tool_sequence.join("|");
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <span className={`pill ${result.reasoning_diverged ? "warn" : "ok"}`}>
          reasoning {result.reasoning_diverged ? "diverged" : "identical"}
        </span>
        <span className={`pill ${result.policy_diverged ? "warn" : "ok"}`}>
          policy {result.policy_diverged ? "diverged" : "identical"}
        </span>
        <span className={`pill ${result.original_actions_unchanged ? "ok" : "danger"}`}>
          original actions {result.original_actions_unchanged ? "unchanged" : "MUTATED"}
        </span>
      </div>

      {result.policy_divergence_cause ? (
        <div className="banner warn">
          <strong>Policy reached a different decision.</strong> {result.policy_divergence_cause}
          {" "}A state divergence is the policy engine working — the world moved between
          the original run and this one.
        </div>
      ) : null}

      <h3>Tool sequence — original against replay</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Original</th><th>Replay</th><th /></tr></thead>
          <tbody>
            {Array.from(
              { length: Math.max(result.original_tool_sequence.length,
                                 result.replay_tool_sequence.length) },
              (_, i) => {
                const a = result.original_tool_sequence[i];
                const b = result.replay_tool_sequence[i];
                return (
                  <tr key={i}>
                    <td className="mono">{i + 1}</td>
                    <td className="mono">{a ?? "—"}</td>
                    <td className="mono">{b ?? "—"}</td>
                    <td>{a === b ? <span className="muted">match</span>
                                 : <span className="pill warn">differs</span>}</td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
      {same ? (
        <p className="sub" style={{ marginTop: 10 }}>
          The same tools in the same order, from the same frozen evidence. That is what
          replay consistency means here — not identical prose.
        </p>
      ) : null}

      {Object.keys(result.diff).length ? (
        <details><summary>diff</summary><pre>{JSON.stringify(result.diff, null, 2)}</pre></details>
      ) : null}
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
