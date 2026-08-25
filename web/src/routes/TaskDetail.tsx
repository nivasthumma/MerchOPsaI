import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { ReplayResult, Task, TraceEvent } from "../api/types";
import { Busy, Empty, ErrorBanner, Money, StatusPill, VerificationPill } from "../components/Bits";

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  const [task, setTask] = useState<Task | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [replay, setReplay] = useState<ReplayResult | null>(null);

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
  if (!task) return <div className="card"><Busy>loading task</Busy></div>;

  const pending = task.approvals.find((a) => a.decision === "PENDING");
  const unsettled = task.actions.filter(
    (a) => a.verification_state === "UNKNOWN" || a.verification_state === "PARTIAL");

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <Link to="/">← Investigate</Link>
      </div>

      <ErrorBanner error={error} />

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>{task.id}</h2>
          <StatusPill status={task.status} />
        </div>
        <p className="sub">{task.request}</p>
        <dl className="kv">
          <dt>Merchant</dt><dd>{task.merchant_id}</dd>
          <dt>User</dt><dd>{task.user_id}</dd>
          <dt>Agent</dt><dd>{task.agent_version}</dd>
          <dt>Model</dt><dd>{task.model_version}</dd>
          <dt>Prompt</dt><dd>{task.prompt_version}</dd>
          {task.failure_code ? (<><dt>Failure</dt><dd>{task.failure_code}</dd></>) : null}
          {task.is_replay ? (<><dt>Replay of</dt><dd>{task.replayed_from}</dd></>) : null}
        </dl>
        {task.final_answer ? <pre style={{ marginTop: 14 }}>{task.final_answer}</pre> : null}
      </div>

      {pending ? (
        <div className="card" style={{ borderLeft: "3px solid var(--warn)" }}>
          <h2>Approval required</h2>
          <p className="sub">
            The policy engine classified this <strong>{pending.risk_level}</strong> risk and
            stopped execution. <strong>No external call has been made.</strong> Authorization
            is re-checked server-side when you approve — this button is a request, not a
            decision.
          </p>
          <dl className="kv">
            <dt>Action</dt><dd>{pending.action_type}</dd>
            <dt>Payment</dt><dd>{String(pending.action_payload.synthetic_payment_id ?? "—")}</dd>
            <dt>Amount</dt>
            <dd><Money minor={pending.action_payload.amount_minor as number | undefined} /></dd>
            <dt>Expires</dt><dd>{new Date(pending.expires_at).toLocaleString()}</dd>
          </dl>
          {typeof pending.action_payload.reason === "string" ? (
            <p style={{ marginTop: 12 }}>{pending.action_payload.reason}</p>
          ) : null}
          <div className="row" style={{ marginTop: 16 }}>
            <button className="primary" disabled={!!busy}
                    onClick={() => act("approve", () => api.approve(task.id))}>
              {busy === "approve" ? "Executing…" : "Approve and execute"}
            </button>
            <button className="danger" disabled={!!busy}
                    onClick={() => act("reject", () => api.reject(task.id))}>
              Reject
            </button>
          </div>
        </div>
      ) : null}

      {task.actions.length ? (
        <div className="card">
          <h2>Actions and verification</h2>
          <p className="sub">
            Verification reads the payment back from the provider. It never trusts the
            response to the request that created it.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Action</th><th>Status</th><th>Payment</th><th>Amount</th>
                  <th>External ref</th><th>Verification</th>
                </tr>
              </thead>
              <tbody>
                {task.actions.map((a) => (
                  <tr key={a.id}>
                    <td>{a.action_type}</td>
                    <td className="mono">{a.status}</td>
                    <td className="mono">{a.target_payment_id ?? "—"}</td>
                    <td><Money minor={a.amount_minor} /></td>
                    <td className="mono">{a.external_reference ?? "—"}</td>
                    <td>
                      <VerificationPill state={a.verification_state} />
                      {a.verification_detail ? (
                        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                          {a.verification_detail}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {unsettled.length ? (
            <div className="banner unknown" style={{ marginTop: 16 }}>
              <strong>{unsettled.length} action{unsettled.length > 1 ? "s" : ""} unsettled.</strong>{" "}
              UNKNOWN is an honest answer, not a failure: the provider may or may not have
              applied it. Re-verification re-reads state by idempotency key and never
              re-issues the action.
              <div className="row" style={{ marginTop: 10 }}>
                <button disabled={!!busy}
                        onClick={() => act("reverify", () => api.reverify(task.id))}>
                  {busy === "reverify" ? "Re-reading…" : "Re-verify"}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="card">
        <h2>Replay</h2>
        <p className="sub">
          Both modes run against frozen tool results and must produce zero external calls.
          That is asserted, not assumed.
        </p>
        <div className="row">
          <button disabled={!!busy}
                  onClick={() => act("playback", async () => setReplay(await api.replay(task.id, "PLAYBACK")))}>
            PLAYBACK
          </button>
          <button disabled={!!busy}
                  onClick={() => act("rereason", async () => setReplay(await api.replay(task.id, "RE_REASON")))}>
            RE_REASON
          </button>
          {busy === "playback" || busy === "rereason" ? <Busy /> : null}
        </div>
        {replay ? (
          <>
            <div className={`banner ${replay.external_calls === 0 ? "info" : "danger"}`}
                 style={{ marginTop: 14 }}>
              <strong>{replay.external_calls} external calls.</strong>{" "}
              {replay.external_calls === 0
                ? "No financial side effect, as required."
                : "This is a defect — replay must never move money."}
              {replay.reasoning_diverged !== undefined
                ? ` Reasoning diverged: ${replay.reasoning_diverged}${
                    replay.divergence_kind ? ` (${replay.divergence_kind})` : ""}.`
                : ""}
            </div>
            <pre>{JSON.stringify(replay, null, 2)}</pre>
          </>
        ) : null}
      </div>

      <div className="card">
        <h2>Audit trace</h2>
        <p className="sub">
          Append-only, enforced by PostgreSQL triggers. Secrets are redacted before write.
        </p>
        {trace.length === 0 ? <Empty>No events.</Empty> : (
          <ul className="trace">
            {trace.map((e) => (
              <li key={e.id}>
                <span className="when">{new Date(e.at).toLocaleTimeString()}</span>
                <span className="what">
                  {e.event}
                  {Object.keys(e.payload ?? {}).length ? (
                    <details>
                      <summary>payload</summary>
                      <pre>{JSON.stringify(e.payload, null, 2)}</pre>
                    </details>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
