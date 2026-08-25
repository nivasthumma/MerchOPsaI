import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EvidenceToolCall, Finding, Task } from "../api/types";
import {
  Busy, CopyId, ErrorBanner, SectionHead, StatStrip, StatusPill,
} from "../components/Bits";
import { ChangeChart, RankChart, type ChangePoint, type RankPoint } from "../components/Charts";
import { EvidencePanel } from "../components/Evidence";
import {
  PolicyOutcome, policyDecisions, type PolicyDecision,
} from "../components/PolicyOutcome";
// The list itself is rendered by the task rail in the app shell; this page only
// writes to it. See recent.ts for why it is a module and not component state.
import { remember } from "../recent";

const EXAMPLES = [
  "Why did revenue drop this week?",
  "Find the duplicate payment and refund it",
  "Which payment method is failing most?",
  "Show me order SYN_ORD_0042",
];

export default function Investigate() {
  // The question lives in the URL so a prepared one can be sent to someone, and
  // so a reload does not discard what was typed. It is a *draft* only: this page
  // never submits from the URL. A link that could create a task would be a link
  // that could attempt a refund, and no link should be able to do that.
  const [params, setParams] = useSearchParams();
  const [request, setRequestState] = useState(params.get("q") ?? "");
  const setRequest = (value: string) => {
    setRequestState(value);
    const next = new URLSearchParams(params);
    if (value.trim()) next.set("q", value);
    else next.delete("q");
    setParams(next, { replace: true });
  };
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [decisions, setDecisions] = useState<PolicyDecision[]>([]);
  const [evidence, setEvidence] = useState<EvidenceToolCall[]>([]);
  const nav = useNavigate();

  // Arriving from the rail's "New investigation" means the next thing you want
  // is to type. Focus only on that signal — stealing focus on every visit would
  // fight anyone who came here to read a previous result.
  const location = useLocation();
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if ((location.state as { focus?: boolean } | null)?.focus) box.current?.focus();
  }, [location.key, location.state]);

  async function submit() {
    setBusy(true);
    setError(null);
    setTask(null);
    setDecisions([]);
    setEvidence([]);
    try {
      const t = await api.createTask(request.trim());
      setTask(t);
      remember(t);
      // What policy decided is the most important thing that happened, and the
      // task payload does not carry it. An analyst asking for a refund gets a
      // COMPLETED task and a tidy report; without this, nothing on the page
      // says the refund was refused.
      void api.getTrace(t.id)
        .then((tr) => setDecisions(policyDecisions(tr.trace)))
        .catch(() => setDecisions([]));
      // The same evidence the task page shows, including merchant-supplied text
      // under quarantine. An injected note is worth seeing wherever the finding
      // that rests on it is shown.
      void api.getEvidence(t.id)
        .then((ev) => setEvidence(ev.tool_calls))
        .catch(() => setEvidence([]));
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
      {/* The composer. The glow sits outside the surface and the text sits on an
          opaque panel, so its contrast is a fixed number rather than a function
          of whatever is behind it. It is the only place in the app that uses
          any of this — below here nothing is translucent. */}
      <div className="ask">
        <div className="ask-label">
          Ask the agent
          <span className="ask-note">typed tools · synthetic data · writes stop at policy</span>
        </div>
        <div className="ask-wrap">
          <div className="ask-box">
            <textarea
              ref={box}
              value={request}
              placeholder="Why did revenue drop this week? · Find the duplicate payment and refund it"
              aria-label="Your question"
              onChange={(e) => setRequest(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
            />
            <div className="ask-foot">
              <button className="primary" onClick={submit} disabled={busy || !request.trim()}
                      aria-busy={busy}>
                {busy ? "Investigating…" : "Investigate"}
                {/* Decorative: the shortcut is announced by the hint text below,
                    and folding it into the button's accessible name renames the
                    button to "Investigate ⌘↵". */}
                <span className="kbd-hint" aria-hidden="true">⌘↵</span>
              </button>
              <button onClick={() => setRequest("")} disabled={busy || !request.trim()}>
                Clear
              </button>
              <span className="ask-hint">
                Anything that moves money stops at the policy engine first — and waits for you.
              </span>
            </div>
          </div>
        </div>
        <div className="examples" aria-label="Example questions">
          {EXAMPLES.map((x) => (
            <button key={x} onClick={() => setRequest(x)} disabled={busy}>{x}</button>
          ))}
        </div>
      </div>

      <div role="alert" aria-live="assertive"><ErrorBanner error={error} /></div>

      {busy ? (
        <div className="card"><Busy>running the agent loop</Busy></div>
      ) : null}

      {task ? <Result task={task} decisions={decisions} evidence={evidence} /> : null}

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

/** Findings named `<method>_success_change_pp` are a polarity series: which
 *  payment methods improved and which fell. Nothing else on the page shows that
 *  at a glance. */
function methodChange(findings: Finding[]): ChangePoint[] {
  const seen = new Map<string, number>();
  for (const f of findings) {
    const m = /^(.+)_success_change_pp$/.exec(f.metric ?? "");
    if (m && typeof f.value === "number" && !seen.has(m[1])) seen.set(m[1], f.value);
  }
  return [...seen.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => a.value - b.value);
}

/** `upi_worst_hours` arrives as formatted strings — "20:00 (75.0% failed)".
 *  Parsed defensively: anything that does not match is dropped rather than
 *  guessed at, and an empty result simply renders no chart. */
function worstHours(findings: Finding[]): RankPoint[] {
  const f = findings.find((x) => x.metric?.endsWith("worst_hours"));
  if (!Array.isArray(f?.value)) return [];
  return (f.value as unknown[]).flatMap((v) => {
    const m = /^(\d{1,2}:\d{2})\s*\(([\d.]+)%\s*failed\)$/.exec(String(v));
    return m ? [{ label: m[1], value: Number(m[2]) }] : [];
  });
}

function Result(
  { task, decisions, evidence }:
  { task: Task; decisions: PolicyDecision[]; evidence: EvidenceToolCall[] },
) {
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
  const change = methodChange(findings);
  const hours = worstHours(findings);

  return (
    <div className="card">
      <SectionHead title="Result">
        <span className="row" style={{ gap: 8 }}>
          <StatusPill status={task.status} />
          {task.failure_code ? (
            <span className="pill danger">{task.failure_code}</span>
          ) : null}
          <CopyId value={task.id} label="task id" />
        </span>
      </SectionHead>

      <PolicyOutcome decisions={decisions} />

      <StatStrip items={[
        ["Tool calls", task.tool_calls ?? 0],
        ["LLM turns", task.llm_turns ?? 0],
        ["Duration", `${task.duration_ms ?? 0} ms`],
        ["Measured", measured.length],
        ["Grounded", `${grounded.length}/${observed.length}`],
      ]} />

      {answer ? (
        <>
          <div className="answer">{answer}</div>
          {conclusion?.evidence_refs?.length ? (
            <p className="muted mono" style={{ fontSize: 12, marginTop: 8 }}>
              {conclusion.kind.toLowerCase()} · grounded in {conclusion.evidence_refs.join(", ")}
            </p>
          ) : null}
        </>
      ) : null}

      {change.length ? (
        <>
          <h3>Where the change landed</h3>
          <ChangeChart points={change} />
        </>
      ) : null}

      {hours.length ? (
        <>
          <h3>When the failures cluster</h3>
          <RankChart points={hours} caption="Share of attempts that failed, by hour" />
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

      {evidence.some((c) => c.evidence.length) ? (
        <details style={{ marginTop: 16 }}>
          <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
            Evidence the agent read ·{" "}
            {evidence.filter((c) => c.evidence.length).length} tool calls
          </summary>
          <p className="sub" style={{ marginTop: 10 }}>
            What the tools returned, with merchant-supplied text quarantined as it was
            when the agent saw it.
          </p>
          <EvidencePanel calls={evidence} />
        </details>
      ) : null}

      <div className="row" style={{ marginTop: 18 }}>
        <Link to={`/tasks/${task.id}`}>Open the full trace, actions and replay →</Link>
      </div>
    </div>
  );
}
