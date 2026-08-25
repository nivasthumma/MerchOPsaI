import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Finding, Task } from "../api/types";
import { Busy, CopyId, ErrorBanner, SectionHead, StatStrip } from "../components/Bits";

const EXAMPLES = [
  "Why did revenue drop this week?",
  "Find the duplicate payment and refund it",
  "Which payment method is failing most?",
  "Show me order SYN_ORD_0042",
];

const RECENT_KEY = "merchantops.recent";

function readRecent(): { id: string; request: string }[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
  } catch {
    return [];
  }
}

function remember(task: Task) {
  // There is no "list my tasks" endpoint — by design, since a task belongs to
  // the merchant rather than to a browser. Keeping the last few locally is a
  // navigation convenience, not a record; the audit trail is server-side.
  try {
    const next = [{ id: task.id, request: task.request },
                  ...readRecent().filter((r) => r.id !== task.id)].slice(0, 5);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* nothing to do */
  }
}

export default function Investigate() {
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [recent, setRecent] = useState(readRecent);
  const nav = useNavigate();

  async function submit() {
    setBusy(true);
    setError(null);
    setTask(null);
    try {
      const t = await api.createTask(request.trim());
      setTask(t);
      remember(t);
      setRecent(readRecent());
      // An approval gate is the interesting case, and it lives on the task page
      // with the payload and the evidence. Go there rather than making the
      // operator find it.
      if (t.status === "AWAITING_APPROVAL") nav(`/tasks/${t.id}`);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Ask the agent</h1>
        <p className="request">
          It investigates with typed tools over synthetic merchant data. Anything that
          moves money stops at the policy engine first — and waits for you.
        </p>
      </div>

      <div className="card">
        <textarea
          value={request}
          placeholder="Why did revenue drop this week?"
          aria-label="Your question"
          onChange={(e) => setRequest(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
        />
        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" onClick={submit} disabled={busy || !request.trim()}
                  aria-busy={busy}>
            {busy ? "Investigating…" : "Investigate"}
          </button>
          <span className="muted"><kbd>⌘</kbd> / <kbd>Ctrl</kbd> + <kbd>↵</kbd></span>
        </div>
        <h3>Try one of these</h3>
        <div className="examples">
          {EXAMPLES.map((x) => (
            <button key={x} onClick={() => setRequest(x)} disabled={busy}>{x}</button>
          ))}
        </div>
      </div>

      <div role="alert" aria-live="assertive"><ErrorBanner error={error} /></div>

      {busy ? (
        <div className="card"><Busy>running the agent loop</Busy></div>
      ) : null}

      {task ? <Result task={task} /> : null}

      {!task && recent.length ? (
        <div className="card">
          <SectionHead title="Recent in this browser" count={`${recent.length}`} />
          <p className="sub">
            Local navigation only. Tasks belong to the merchant and live server-side; this
            list is not the record.
          </p>
          <ul>
            {recent.map((r) => (
              <li key={r.id}>
                <Link to={`/tasks/${r.id}`}>{r.id}</Link>{" "}
                <span className="muted">— {r.request}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

/** `value` is whatever a tool produced: number, formatted string, or list.
 *  Rendering it straight into JSX is how the task page once crashed. */
function formatValue(v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.map(formatValue).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** One row per metric, with every tool call that produced it. The agent cites
 *  the same metric from more than one tool — `card_success_change_pp` arrives
 *  from both the metrics call and the drill-down — and listing it twice reads
 *  as two findings when it is one fact with two citations. */
function groupMeasured(findings: Finding[]) {
  const rows = new Map<string, { metric: string; value: unknown; refs: string[] }>();
  for (const f of findings) {
    if (!f.metric) continue;
    const row = rows.get(f.metric);
    if (row) row.refs.push(...(f.evidence_refs ?? []));
    else rows.set(f.metric, { metric: f.metric, value: f.value, refs: [...(f.evidence_refs ?? [])] });
  }
  return [...rows.values()].map((r) => ({ ...r, refs: [...new Set(r.refs)] }));
}

function Result({ task }: { task: Task }) {
  const findings = task.findings ?? [];
  const measured = groupMeasured(findings);
  const observed = findings.filter((f) => f.kind === "OBSERVED");
  const grounded = observed.filter((f) => (f.evidence_refs?.length ?? 0) > 0);

  // The agent's concluding finding carries the same prose as `final_answer`,
  // so printing both puts the same paragraph on the page twice. Keep the
  // answer, and lift that finding's citations onto it — the conclusion is the
  // one claim most worth showing evidence for.
  const answer = task.final_answer ?? "";
  const conclusion = findings.find((f) => !f.metric && f.claim.trim() === answer.trim());
  const narrative = findings.filter((f) => !f.metric && f !== conclusion);

  return (
    <div className="card">
      <SectionHead title="Result">
        <CopyId value={task.id} label="task id" />
      </SectionHead>

      <StatStrip items={[
        ["Tool calls", task.tool_calls ?? 0],
        ["LLM turns", task.llm_turns ?? 0],
        ["Duration", `${task.duration_ms ?? 0} ms`],
        ["Measured", measured.length],
        ["Grounded", `${grounded.length}/${observed.length}`],
      ]} />

      {answer ? (
        <>
          <pre>{answer}</pre>
          {conclusion?.evidence_refs?.length ? (
            <p className="muted mono" style={{ fontSize: 12, marginTop: 8 }}>
              {conclusion.kind.toLowerCase()} · grounded in {conclusion.evidence_refs.join(", ")}
            </p>
          ) : null}
        </>
      ) : null}

      {measured.length ? (
        <>
          <h3>Evidence — measured, with the tool call that produced it</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Metric</th><th>Value</th><th>Cited from</th></tr></thead>
              <tbody>
                {measured.map((m) => (
                  <tr key={m.metric}>
                    <td className="mono">{m.metric}</td>
                    <td className="mono">{formatValue(m.value)}</td>
                    <td className="mono muted">
                      {m.refs.length ? m.refs.join(", ") : <span className="pill danger">ungrounded</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {narrative.length ? (
        <>
          <h3>Inference and recommendation</h3>
          <ul>
            {narrative.map((f, i) => (
              <li key={i}>
                {f.claim}{" "}
                <span className="pill neutral">{f.kind.toLowerCase()}</span>
                {f.evidence_refs?.length ? (
                  <div className="muted mono" style={{ fontSize: 12 }}>
                    from {f.evidence_refs.join(", ")}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <div className="row" style={{ marginTop: 18 }}>
        <Link to={`/tasks/${task.id}`}>Open the full trace, actions and replay →</Link>
      </div>
    </div>
  );
}
