import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type {
  AgentMessage,
  Approval, EvidenceToolCall, PlaybackResult, Principal, ReplayResult, ReReasonResult, Task,
  TraceEvent,
} from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip,
  StatusPill, VerificationPill, When,
} from "../components/Bits";
import { EvidencePanel } from "../components/Evidence";
import { forgetOne } from "../recent";
import { PolicyOutcome, policyDecisions } from "../components/PolicyOutcome";
import { Stepper } from "../components/Stepper";
import { useToast } from "../components/Toast";
import { groupOf, iconOf, summarise, type TraceGroup } from "./trace-summary";

/** The panes below the gate. Everything the old page stacked vertically is
 *  here; none of it was dropped, it is one click instead of one scroll. */
type Pane = "trace" | "evidence" | "actions" | "history" | "replay" | "transcript";
const PANES: [Pane, string][] = [
  ["trace", "Trace"], ["transcript", "Transcript"], ["evidence", "Evidence"],
  ["actions", "Actions"], ["history", "Approvals"], ["replay", "Replay"],
];

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  // Optional-chained: the route also renders in tests that mount it bare, with
  // no outlet context above it.
  const me = useOutletContext<{ me: Principal | null } | null>()?.me ?? null;
  const nav = useNavigate();
  const [task, setTask] = useState<Task | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [evidence, setEvidence] = useState<EvidenceToolCall[]>([]);
  const [live, setLive] = useState<"live" | "hidden" | "idle">("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [replay, setReplay] = useState<{ mode: string; result: ReplayResult } | null>(null);
  // CONTRACT §21 has the human review the evidence before approving. Evidence
  // behind an unopened tab is evidence nobody read, so while an approval is
  // pending the evidence pane is what is showing — beside the gate, on screen,
  // without a scroll. Once nothing is waiting, the trace is the useful default.
  const [pane, setPane] = useState<Pane | null>(null);
  const toast = useToast();

  const load = useCallback(async (quiet = false) => {
    try {
      const [t, tr, ev] = await Promise.all([
        api.getTask(taskId), api.getTrace(taskId), api.getEvidence(taskId),
      ]);
      setTask(t);
      setTrace(tr.trace);
      setEvidence(ev.tool_calls);
      if (!quiet) setError(null);
    } catch (e) {
      // A failed background poll must not replace a page that is working.
      if (!quiet) setError(e as ApiError);
    }
  }, [taskId]);

  useEffect(() => { void load(); }, [load]);

  // A task waiting on a human, or holding an action the sweep may settle from
  // somewhere else entirely, is not finished. Poll it — quietly, only while the
  // tab is visible, and never once nothing can change.
  const settling = task
    ? task.status === "AWAITING_APPROVAL" || task.status === "RUNNING" ||
      task.actions.some((a) => a.verification_state === "UNKNOWN" ||
                              a.verification_state === "PARTIAL")
    : false;

  useEffect(() => {
    if (!settling) { setLive("idle"); return; }
    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = () => {
      if (document.hidden) { setLive("hidden"); return; }
      setLive("live");
      void load(true);
    };
    const start = () => { if (!timer) timer = setInterval(tick, 5000); };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };

    const onVisibility = () => {
      if (document.hidden) { setLive("hidden"); stop(); }
      else { setLive("live"); void load(true); start(); }
    };

    setLive(document.hidden ? "hidden" : "live");
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [settling, load]);

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

  if (error && !task) {
    // A 404 here usually means the rail is pointing at something the server no
    // longer has: the list lives in this browser, the record lives in the
    // database, and a reseed separates them. Saying "Unknown task" and stopping
    // leaves a dead link in the rail forever.
    const gone = error.status === 404;
    return (
      <div className="card">
        <ErrorBanner error={error} />
        {gone ? (
          <p className="sub" style={{ marginTop: 12 }}>
            This task is not on the server. Recent tasks are remembered in this
            browser only — the authoritative record is server-side, so reseeding
            the database leaves entries here pointing at nothing.
          </p>
        ) : null}
        <div className="row" style={{ marginTop: 12 }}>
          <button onClick={() => { setError(null); void load(); }}>Try again</button>
          {gone ? (
            <button className="primary"
                    onClick={() => { forgetOne(taskId); nav("/"); }}>
              Remove from list
            </button>
          ) : null}
        </div>
      </div>
    );
  }
  if (!task) {
    return (
      <div className="card">
        <Skeleton rows={4} />
      </div>
    );
  }

  const pending = task.approvals.find((a) => a.decision === "PENDING");
  const shown: Pane = pane ?? (pending ? "evidence" : "trace");
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
          <LiveDot state={live} />
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

      <Conclusion task={task} />
      <FailureDetail failure={task.failure} />

      {/* One screen, no page scroll. The gate is always visible; everything
          else lives in a pane below it that scrolls inside itself. The reason
          is not tidiness: on a page that scrolls, the approval button moves,
          and a button that moves is a button that gets mis-clicked. */}
      <div className="task-layout">
      <div className="task-main">

      {/* Announced rather than only shown: an operator acting on a refund needs
          to hear a refusal even when focus is elsewhere on the page. */}
      <div role="alert" aria-live="assertive">
        <ErrorBanner error={error} />
      </div>

      <PolicyOutcome decisions={policyDecisions(trace)} />

      {pending ? (
        <ApprovalGate
          approval={pending} busy={busy}
          signer={me?.user_id ?? "this session"}
          onApprove={() => act("approve", () => api.approve(task.id), (t2) => {
            const v = t2.actions[t2.actions.length - 1]?.verification_state;
            return ["Approved and executed",
                    v ? `Independent verification: ${v}` : "Verification pending"];
          })}
          onReject={() => act("reject", () => api.reject(task.id),
                              () => ["Rejected", "No external call was made."])}
        />
      ) : null}

      {/* Pane deck. Each pane scrolls inside itself so the page never does. */}
      <div className="deck">
        <div className="deck-tabs" role="tablist" aria-label="Task detail">
          {PANES.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={shown === id}
                    className={shown === id ? "on" : ""}
                    onClick={() => setPane(id)}>
              {label}
              {id === "evidence" && evidence.length
                ? <span className="n">{evidence.length}</span> : null}
              {id === "actions" && task.actions.length
                ? <span className="n">{task.actions.length}</span> : null}
              {id === "history" && decided.length
                ? <span className="n">{decided.length}</span> : null}
            </button>
          ))}
        </div>

        <div className="deck-body" role="tabpanel">
          {shown === "transcript" ? <TranscriptPanel taskId={task.id} /> : null}

          {shown === "trace" ? <TracePanel events={trace} /> : null}

          {shown === "trace" && task.final_answer ? (
        <div className="card">
          <SectionHead title="Answer" />
          <div className="answer">{task.final_answer}</div>
        </div>
      ) : null}

          {shown === "actions" && !task.actions.length ? (
            <Empty>
              Nothing has executed. An action appears here only after an approval
              is granted — the request above is still at the gate.
            </Empty>
          ) : null}

          {shown === "actions" && task.actions.length ? (
        <div className="card">
          <SectionHead title="Actions and verification" count={`${task.actions.length}`} />
          <p className="sub">
            Verification reads the payment back from the provider. It never trusts the
            response to the request that created it.
          </p>

          {/* A ledger, not a stack of cards: one row per action, columns aligned,
              amount right-aligned. Anything that needs more than a row — the
              verification reason, expected vs actual — sits directly under its
              own row rather than being hidden behind it. */}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Action</th><th>Target</th><th className="r">Amount</th>
                  <th>Status</th><th>Verified</th><th>Reference</th>
                </tr>
              </thead>
                {/* One tbody per action — legal HTML, and it keeps each action's
                    row and its verification note as one addressable block. */}
                {task.actions.map((a) => (
                  <tbody className="action-card" key={a.id}>
                    <tr>
                      <td className="mono">{a.action_type}</td>
                      <td className="mono">{a.target_payment_id ?? "—"}</td>
                      <td className="r mono"><Money minor={a.amount_minor} /></td>
                      <td><span className="pill neutral">{a.status}</span></td>
                      <td><VerificationPill state={a.verification_state} /></td>
                      <td className="mono">
                        {a.external_reference
                          ? <CopyId value={a.external_reference} label="external reference" />
                          : <span className="muted">none received</span>}
                      </td>
                    </tr>
                    {a.verification_detail ? (
                      <tr className="row-note">
                        <td colSpan={6}>
                          <p className="reason">
                            {a.verification_detail.reason}
                            {a.verify_attempts > 0 ? (
                              <span className="muted">
                                {" "}· {a.verify_attempts} verify attempt
                                {a.verify_attempts > 1 ? "s" : ""}
                              </span>
                            ) : null}
                          </p>
                          {a.verification_detail.expected || a.verification_detail.actual ? (
                            <details>
                              <summary>expected vs actual</summary>
                              <pre>{JSON.stringify(
                                { expected: a.verification_detail.expected,
                                  actual: a.verification_detail.actual }, null, 2)}</pre>
                            </details>
                          ) : null}
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                ))}
            </table>
          </div>

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

          {shown === "evidence" && !evidence.some((c) => c.evidence.length) ? (
            <Empty>
              No tool call returned evidence. Nothing here is being withheld —
              there is nothing to show.
            </Empty>
          ) : null}

          {shown === "evidence" && evidence.some((c) => c.evidence.length) ? (
        <div className="card">
          <SectionHead title="Evidence this rests on"
                       count={`${evidence.filter((c) => c.evidence.length).length} tool calls`} />
          <p className="sub">
            CONTRACT §21 has the human review the payment, the amount, the reason,
            <strong> the evidence</strong> and the risk. What the tools actually returned,
            including any merchant-supplied text — quarantined here as it was when the
            agent saw it: it is evidence, and it is also the injection surface.
          </p>
          <EvidencePanel calls={evidence} />
        </div>
      ) : null}

          {shown === "history" && !decided.length ? (
            <Empty>
              No decision has been recorded yet. Approving or rejecting above
              writes a signed entry here, and it is never removed.
            </Empty>
          ) : null}

          {shown === "history" && decided.length ? (
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

          {shown === "replay" ? (
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
          ) : null}

        </div>
      </div>

      </div>
      <TaskRail evidence={evidence} decisions={policyDecisions(trace)} />
      </div>
    </>
  );
}

/** What the decision rests on, in one narrow column: the evidence, the rules
 *  that fired, and the actions. It is a summary and says so — every row here is
 *  also rendered in full in the main column, because a rail is too narrow to be
 *  the place someone reads merchant-supplied text before approving a refund. */
function TaskRail(
  { evidence, decisions }:
  { evidence: EvidenceToolCall[]; decisions: ReturnType<typeof policyDecisions> },
) {
  const grounded = evidence.filter((c) => c.evidence.length);
  return (
    <aside className="task-rail" aria-label="What this rests on">
      <div className="rail-sec">
        <div className="rail-sec-head">Evidence<span>{grounded.length}</span></div>
        {grounded.length === 0
          ? <p className="rail-none">No tool call returned evidence.</p>
          : grounded.map((c) => (
              <div className="rail-row" key={c.id}>
                <span className="nm">{c.tool}</span>
                <span className="rr">{c.evidence.length}</span>
              </div>
            ))}
      </div>

      <div className="rail-sec">
        <div className="rail-sec-head">Policy engine<span>{decisions.length}</span></div>
        {decisions.length === 0
          ? <p className="rail-none">No policy decision recorded yet.</p>
          : decisions.map((d, i) => (
              <div className="rail-row" key={`${d.tool}-${i}`}>
                <span className="nm">{d.rule ?? d.tool}</span>
                <span className={`rr d-${d.decision}`}>{d.decision}</span>
              </div>
            ))}
      </div>

      <div className="rail-sec">
        <div className="rail-sec-head">Tools called<span>{evidence.length}</span></div>
        {evidence.length === 0
          ? <p className="rail-none">No tool was called.</p>
          : evidence.map((c) => (
              <div className="rail-row" key={`t-${c.id}`}>
                <span className="nm">{c.tool}</span>
                <span className={`rr ${c.success ? "" : "d-DENY"}`}>
                  {c.success ? `${c.duration_ms}ms` : "ERR"}
                </span>
              </div>
            ))}
      </div>

    </aside>
  );
}

function LiveDot({ state }: { state: "live" | "hidden" | "idle" }) {
  if (state === "idle") return null;
  return (
    <span className={`pill ${state === "live" ? "ok" : "neutral"}`}
          title={state === "live"
            ? "Refreshing every 5 seconds while this task can still change"
            : "Paused — this tab is in the background"}>
      {state === "live" ? "live" : "paused"}
    </span>
  );
}

function ApprovalGate(
  { approval, busy, signer, onApprove, onReject }:
  { approval: Approval; busy: string | null;
    signer: string; onApprove: () => void; onReject: () => void },
) {
  const expires = new Date(approval.expires_at);
  const expired = expires.getTime() < Date.now();
  // Approval is two-step on purpose: a gate you can clear by reflex is not a
  // gate. It arms locally and disarms itself; the server-side authorization
  // check on approve is unchanged and remains the real authority.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(t);
  }, [armed]);
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
      {/* The amount does not belong in a definition list. It is the one value a
          person can most expensively misread, so it is promoted out and set at
          display size, with the destination spelled out beside it. */}
      <div className="gate-amount">
        <div>
          <span className="k">Amount</span>
          <span className="fig">
            <Money minor={approval.action_payload.amount_minor as number | undefined} />
          </span>
        </div>
        <div className="where">
          leaves the merchant balance if you approve.
          <strong> Nothing has been sent yet</strong> — the policy engine stopped
          execution before the call, and this page has made none.
        </div>
      </div>
      <dl className="kv">
        <dt>Action</dt><dd>{approval.action_type}</dd>
        <dt>Payment</dt><dd>{String(approval.action_payload.synthetic_payment_id ?? "—")}</dd>
        <dt>Expires</dt>
        <dd>
          <When iso={approval.expires_at} />
          {expired ? <span className="pill danger" style={{ marginLeft: 8 }}>expired</span> : null}
        </dd>
      </dl>
      {typeof approval.action_payload.reason === "string" ? (
        <p style={{ marginTop: 12 }}>{approval.action_payload.reason}</p>
      ) : null}
      {/* CONTRACT §21 has the human review the payment, the amount, the reason,
          the evidence and the risk. The evidence is directly below this card
          rather than inside it: with seven duplicate rows and four quarantined
          notes in here, the button you need sat below the fold, which is its
          own kind of failure. It is still on the same screen and still before
          any approval can be made. */}
      {expired ? (
        <div className="banner warn" style={{ marginTop: 12 }}>
          This approval has passed its expiry. The server will refuse it — the button is
          left enabled so the refusal comes from the authority, not from this page.
        </div>
      ) : null}
      <div className="row" style={{ marginTop: 16 }}>
        <button className={`primary${armed ? " armed" : ""}`} disabled={!!busy}
                aria-busy={busy === "approve"}
                onClick={() => { if (armed) { setArmed(false); onApprove(); } else setArmed(true); }}>
          {busy === "approve"
            ? "Executing…"
            : armed ? "Confirm — this moves money" : "Approve and execute"}
        </button>
        <button className="danger" disabled={!!busy} onClick={onReject}>Reject</button>
        <span className="gate-sign">
          <strong>{signer}</strong>SIGNED → AUDIT LOG
        </span>
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

/** MerchantOps §37. The model's own typed output, which the task carried and
 *  this page did not show.
 *
 *  `confidence` is rendered as what it is — a number the model reported —
 *  rather than as a quality score, because it is consulted by nothing. And
 *  `requires_human` is the OR of policy and the model: the model may raise the
 *  bar and never lower it, so a model saying "no" next to a pending approval
 *  would read as a disagreement it is not entitled to have. */
function Conclusion({ task }: { task: Task }) {
  if (!task.intent && !task.recommendation && task.agent_confidence == null) return null;
  return (
    <div className="card conclusion">
      <SectionHead title="What the agent concluded" />
      <StatStrip items={[
        ["Intent", task.intent ?? <span className="muted">—</span>],
        ["Recommends", task.recommendation
          ? <code>{task.recommendation.type}</code>
          : <span className="muted">no action</span>],
        ["Model confidence", task.agent_confidence == null
          ? <span className="muted">—</span>
          : <span title="Reported by the model. Consulted by nothing.">
              {task.agent_confidence.toFixed(2)}
            </span>],
        ["Human required", task.requires_human ? "yes" : "no"],
      ]} />
      {task.recommendation?.detail
        ? <p className="muted">{task.recommendation.detail}</p> : null}
      {task.requires_human && !task.model_requires_human ? (
        <p className="muted">
          Policy requires a human here; the model did not ask for one. The model
          can raise that bar and never lower it.
        </p>
      ) : null}
    </div>
  );
}

/** MerchantOps §56. A code says what broke. It does not say whether trying
 *  again is sensible, which is the question an operator actually has. */
function FailureDetail({ failure }: { failure: Task["failure"] }) {
  if (!failure) return null;
  // No client-side copy of what each retryability means. The backend already
  // sends `recommended_next_action` per code, and a second phrasing here would
  // be a second source of truth that drifts — and drifted immediately: the two
  // rendered the same sentence twice.
  return (
    <div className="card failure" data-retry={failure.retryability}>
      <SectionHead title="Failure">
        <code>{failure.category}</code>
      </SectionHead>
      <StatStrip items={[
        ["Code", <code key="c">{failure.error_code}</code>],
        ["Owner", failure.owning_subsystem],
        ["Retry", failure.retryability],
      ]} />
      <p><strong>{failure.recommended_next_action}</strong></p>
      {!failure.is_classified ? (
        <p className="muted">
          This code is not in the failure taxonomy, so it is treated as an
          internal error and escalates. That is a gap in the taxonomy, not a
          transient condition.
        </p>
      ) : null}
    </div>
  );
}

/** MerchantOps §66. What the model was looking at, as distinct from what the
 *  application did — which is the trace, one tab over. */
function TranscriptPanel({ taskId }: { taskId: string }) {
  const [rows, setRows] = useState<AgentMessage[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    api.getMessages(taskId)
      .then((r) => { if (!cancelled) setRows(r.messages); })
      .catch((e) => { if (!cancelled) setError(e); });
    return () => { cancelled = true; };
  }, [taskId]);

  if (error) return <ErrorBanner error={error} />;
  if (!rows) return <Skeleton rows={4} />;
  if (!rows.length) return <Empty>No conversation was recorded.</Empty>;

  return (
    <div className="card">
      <SectionHead title="What the model saw" count={`${rows.length}`} />
      <ol className="transcript" aria-label="Transcript">
        {rows.map((m) => (
          <li key={m.seq} data-role={m.role} data-untrusted={m.contains_untrusted || undefined}>
            <div className="meta">
              <span className="role">{m.role}</span>
              <span className="muted">turn {m.turn} · {m.char_count} chars</span>
              {m.contains_untrusted ? (
                <span className="tag warn"
                      title="Merchant free text. Quarantined when the model saw it.">
                  untrusted
                </span>
              ) : null}
            </div>
            <pre>{JSON.stringify(m.content, null, 2)}</pre>
          </li>
        ))}
      </ol>
    </div>
  );
}

function TracePanel({ events }: { events: TraceEvent[] }) {
  // In the URL rather than in state: "look at the verification stage of this
  // task" should be a link you can paste to someone, and a reload should not
  // throw away the filter an operator just set.
  const [params, setParams] = useSearchParams();
  const filter = (params.get("stage") ?? "all") as TraceGroup | "all";
  const query = params.get("q") ?? "";

  const update = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v && v !== "all") next.set(k, v);
      else next.delete(k);
    }
    setParams(next, { replace: true });
  };

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter((e) => {
      if (filter !== "all" && groupOf(e.event) !== filter) return false;
      if (!q) return true;
      return e.event.toLowerCase().includes(q)
        || (summarise(e) ?? "").toLowerCase().includes(q)
        || JSON.stringify(e.payload ?? {}).toLowerCase().includes(q);
    });
  }, [events, filter, query]);

  return (
    <div className="card">
      <SectionHead title="Audit trace" count={`${shown.length} of ${events.length}`} />
      <p className="sub">
        Append-only, enforced by PostgreSQL triggers. Secrets are redacted before write.
        This is the primary record of what the system did.
      </p>
      <div className="filters" role="group" aria-label="Filter trace by stage">
        {FILTERS.map((f) => (
          <button key={f.key} aria-pressed={filter === f.key}
                  onClick={() => update({ stage: f.key })}>
            {f.label}
          </button>
        ))}
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
        <input type="text" value={query} placeholder="Search events, summaries and payloads…"
               aria-label="Search the trace" style={{ maxWidth: 340 }}
               onChange={(e) => update({ q: e.target.value })} />
        {query || filter !== "all" ? (
          <button onClick={() => update({ stage: "all", q: "" })}>Clear</button>
        ) : null}
        <span className="muted" style={{ marginLeft: "auto" }}>
          <button onClick={() => navigator.clipboard?.writeText(
            JSON.stringify(events, null, 2))}>Copy trace JSON</button>
        </span>
      </div>
      {shown.length === 0 ? (
        <Empty>
          {query ? `Nothing in this trace matches “${query}”.` : "No events at this stage."}
        </Empty>
      ) : (
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
