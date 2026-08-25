import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Task } from "../api/types";
import { Busy, ErrorBanner, StatusPill } from "../components/Bits";

const EXAMPLES = [
  "Why did revenue drop this week?",
  "Find the duplicate payment and refund it",
  "Which payment method is failing most?",
  "Show me order SYN_ORD_0042",
];

export default function Investigate() {
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const nav = useNavigate();

  async function submit() {
    setBusy(true);
    setError(null);
    setTask(null);
    try {
      const t = await api.createTask(request.trim());
      setTask(t);
      // An approval gate is the interesting case, and it lives on the task
      // page with the payload and the evidence. Go there rather than making
      // the operator find it.
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

      <ErrorBanner error={error} />
      {busy ? <div className="card"><Busy>running the agent loop</Busy></div> : null}
      {task ? <Result task={task} /> : null}
    </>
  );
}

function Result({ task }: { task: Task }) {
  const observed = (task.findings ?? []).filter((f) => f.kind === "OBSERVED");
  const inferred = (task.findings ?? []).filter((f) => f.kind !== "OBSERVED");
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>{task.id}</h2>
        <StatusPill status={task.status} />
      </div>
      <p className="sub">
        {task.tool_calls ?? 0} tool calls · {task.llm_turns ?? 0} turns ·{" "}
        {task.duration_ms ?? 0} ms · prompt <code>{task.prompt_version}</code>
      </p>

      {task.final_answer ? <pre>{task.final_answer}</pre> : null}

      {observed.length ? (
        <>
          <h3>Observed — grounded in tool output</h3>
          <ul>
            {observed.map((f, i) => (
              <li key={i}>
                {f.claim}{" "}
                {f.evidence_refs?.length ? (
                  <span className="muted mono">[{f.evidence_refs.join(", ")}]</span>
                ) : (
                  <span className="pill danger">ungrounded</span>
                )}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {inferred.length ? (
        <>
          <h3>Inferred and recommended</h3>
          <ul>{inferred.map((f, i) => <li key={i}>{f.claim} <span className="muted">({f.kind.toLowerCase()})</span></li>)}</ul>
        </>
      ) : null}

      <div className="row" style={{ marginTop: 16 }}>
        <a href={`/tasks/${task.id}`}>Open task · trace, actions, replay →</a>
      </div>
    </div>
  );
}
