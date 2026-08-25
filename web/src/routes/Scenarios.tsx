import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Scenario, ScenarioResult } from "../api/types";
import { Busy, ErrorBanner, SectionHead, Skeleton, StatStrip } from "../components/Bits";
import { assertions, conditions } from "./scenario-language";

export default function Scenarios() {
  const [all, setAll] = useState<Scenario[] | null>(null);
  const [params, setParams] = useSearchParams();
  const category = params.get("category") ?? "all";
  const criticalOnly = params.get("critical") === "1";
  const query = params.get("q") ?? "";
  const setParam = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v && v !== "all") next.set(k, v);
      else next.delete(k);
    }
    setParams(next, { replace: true });
  };

  // A run-all over a filtered set takes minutes, because each scenario reseeds
  // the database server-side. Losing that to a reload would be its own reason
  // not to use the page.
  const [results, setResults] = useState<Record<string, ScenarioResult>>(() => {
    try {
      return JSON.parse(sessionStorage.getItem("merchantops.scenarioRuns") ?? "{}");
    } catch {
      return {};
    }
  });

  useEffect(() => {
    try {
      sessionStorage.setItem("merchantops.scenarioRuns", JSON.stringify(results));
    } catch {
      /* results simply will not survive a reload */
    }
  }, [results]);
  const [running, setRunning] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const stop = useRef(false);

  useEffect(() => {
    api.scenarios().then(setAll).catch((e) => setError(e as ApiError));
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of all ?? []) c[s.category] = (c[s.category] ?? 0) + 1;
    return c;
  }, [all]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = (all ?? []).filter((s) =>
      (category === "all" || s.category === category)
      && (!criticalOnly || s.critical)
      && (!q || s.id.toLowerCase().includes(q)
              || s.description.toLowerCase().includes(q)
              || s.request.toLowerCase().includes(q)
              || JSON.stringify(s.expect).toLowerCase().includes(q)));
    // After a run, failures come first: a red result at the bottom of a
    // hundred-row table is a result nobody sees.
    return [...rows].sort((a, b) => {
      const ra = results[a.id], rb = results[b.id];
      const rank = (r?: ScenarioResult) => (r ? (r.passed ? 1 : 0) : 2);
      return rank(ra) - rank(rb);
    });
  }, [all, category, criticalOnly, query, results]);

  const ran = Object.values(results);
  const failed = ran.filter((r) => !r.passed);

  async function run(id: string) {
    setRunning(id);
    setError(null);
    try {
      const result = await api.runScenario(id);
      setResults((r) => ({ ...r, [id]: result }));
      return result;
    } catch (e) {
      setError(e as ApiError);
      return null;
    } finally {
      setRunning(null);
    }
  }

  async function runAll() {
    // Each scenario runs against a freshly seeded database server-side, so this
    // is seconds per scenario, not milliseconds. Say so before starting rather
    // than leaving someone watching a spinner.
    if (shown.length > 25 &&
        !window.confirm(
          `Run ${shown.length} scenarios? Each reseeds the database server-side, ` +
          `so this takes roughly ${Math.ceil(shown.length * 1.5 / 60)} minute(s).`)) return;
    stop.current = false;
    setProgress({ done: 0, total: shown.length });
    for (const [i, s] of shown.entries()) {
      if (stop.current) break;
      await run(s.id);
      setProgress({ done: i + 1, total: shown.length });
    }
    setProgress(null);
  }

  return (
    <>
      <div className="page-head">
        <h1>Evaluation scenarios</h1>
        <p className="request">
          Graded on <strong>observable behaviour</strong> — tool sequence, policy decision,
          final status, verification state, and whether an external financial effect
          occurred. Never on prose.
        </p>
      </div>

      <div role="alert" aria-live="assertive"><ErrorBanner error={error} /></div>

      <div className="banner warn">
        <strong>Runs here are not the published suite.</strong> <code>make eval</code>
        reseeds the database before <em>every</em> scenario, which is what makes 106/106
        reproducible. This endpoint runs against the database as it stands right now, so a
        scenario can fail here for a reason that has nothing to do with the code — a
        duplicate already refunded by an earlier task, for instance, is correctly denied
        the second time. Treat a failure here as a prompt to run{" "}
        <code>make eval</code>, not as a result.
      </div>

      {all === null ? <div className="card"><Skeleton rows={3} /></div> : (
        <>
          <StatStrip items={[
            ["Scenarios", all.length],
            ["Critical", all.filter((s) => s.critical).length],
            ["Categories", Object.keys(counts).length],
            ["Run here", ran.length],
            ["Ran on", ran.length
              ? [...new Set(ran.map((r) => r.provider ?? "?"))].join(", ")
              : "—"],
            ["Failed", failed.length === 0 && ran.length > 0
              ? <span className="pill ok">none</span>
              : <span className={failed.length ? "pill danger" : ""}>{failed.length}</span>],
          ]} />

          <div className="card">
            <div className="filters" role="group" aria-label="Filter by category">
              <button aria-pressed={category === "all"} onClick={() => setParam({ category: "all" })}>
                Everything <span className="muted">{all.length}</span>
              </button>
              {Object.entries(counts).sort().map(([c, n]) => (
                <button key={c} aria-pressed={category === c}
                        onClick={() => setParam({ category: c })}>
                  {c.replace(/_/g, " ")} <span className="muted">{n}</span>
                </button>
              ))}
            </div>
            <div className="row" style={{ marginBottom: 10 }}>
              <input type="text" value={query} aria-label="Search scenarios"
                     placeholder="Search id, description, request or assertions…"
                     style={{ maxWidth: 360 }}
                     onChange={(e) => setParam({ q: e.target.value })} />
              {query || category !== "all" || criticalOnly ? (
                <button onClick={() => setParam({ category: "all", critical: "", q: "" })}>
                  Clear
                </button>
              ) : null}
            </div>
            <div className="row">
              <label style={{ margin: 0 }}>
                <input type="checkbox" checked={criticalOnly}
                       onChange={(e) => setParam({ critical: e.target.checked ? "1" : "" })} />{" "}
                critical only
              </label>
              <span className="muted">{shown.length} shown</span>
              <span style={{ marginLeft: "auto" }} />
              {progress ? (
                <>
                  <Busy>{`${progress.done}/${progress.total}`}</Busy>
                  <button className="danger" onClick={() => { stop.current = true; }}>Stop</button>
                </>
              ) : (
                <button onClick={runAll} disabled={!!running || shown.length === 0}>
                  Run all shown
                </button>
              )}
            </div>
          </div>

          <div className="card">
            <SectionHead title="Scenarios" count={`${shown.length}`} />
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>ID</th><th>Description</th><th>Result</th><th /></tr>
                </thead>
                <tbody>
                  {shown.map((s) => (
                    <Row key={s.id} scenario={s} result={results[s.id]}
                         running={running === s.id} disabled={!!running || !!progress}
                         onRun={() => run(s.id)} />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function Row(
  { scenario, result, running, disabled, onRun }:
  { scenario: Scenario; result?: ScenarioResult; running: boolean;
    disabled: boolean; onRun: () => void },
) {
  const m = result?.metrics;
  const asserts = assertions(scenario.expect);
  const setup = conditions(scenario.setup);

  return (
    <tr>
      <td className="mono" style={{ whiteSpace: "nowrap", verticalAlign: "top" }}>
        {scenario.id}
        {scenario.critical ? (
          <div><span className="pill danger">critical</span></div>
        ) : null}
        <div className="muted" style={{ fontSize: 11 }}>
          {scenario.category.replace(/_/g, " ")}
        </div>
        {scenario.principal !== "owner" ? (
          <div><span className="pill neutral">as {scenario.principal}</span></div>
        ) : null}
      </td>

      <td style={{ maxWidth: 460, verticalAlign: "top" }}>
        {scenario.description}
        <details style={{ marginTop: 6 }}>
          <summary className="muted" style={{ fontSize: 12 }}>
            what it asserts · {asserts.length}
          </summary>
          <div className="assert-block">
            <div className="assert-line">
              <span className="k">request</span>
              <span className="mono">“{scenario.request}”</span>
            </div>
            <div className="assert-line">
              <span className="k">runs as</span>
              <span className="mono">{scenario.principal}</span>
            </div>
            {setup.length ? (
              <div className="assert-line">
                <span className="k">setup</span>
                <span>{setup.join(" · ")}</span>
              </div>
            ) : null}
            <ul className="asserts">
              {asserts.map((a) => <li key={a}>{a}</li>)}
            </ul>
          </div>
        </details>
      </td>

      <td style={{ verticalAlign: "top" }}>
        {running ? <Busy>running</Busy>
          : result ? (
            <>
              <span className={`pill ${result.passed ? "ok" : "danger"}`}>
                {result.passed ? "pass" : "fail"}
              </span>
              {result.provider && result.provider !== "deterministic" ? (
                <div><span className="pill warn">ran on {result.model}</span></div>
              ) : null}
              {m ? (
                <div className="muted metrics" style={{ fontSize: 12, marginTop: 4 }}>
                  {m.final_status} · {m.tool_calls} tools · {m.duration_ms} ms
                  {m.external_actions > 0 ? (
                    <div><span className="pill warn">{m.external_actions} external action
                      {m.external_actions > 1 ? "s" : ""}</span></div>
                  ) : null}
                  {m.verification_states.length ? (
                    <div>verification: {m.verification_states.join(", ")}</div>
                  ) : null}
                </div>
              ) : null}
              {result.task_id ? (
                <div style={{ marginTop: 6 }}>
                  <Link to={`/tasks/${result.task_id}`}>
                    {result.passed ? "Open the task" : "Open the task to see why"} →
                  </Link>
                </div>
              ) : null}
              <details>
                <summary>{result.checks.length} checks</summary>
                <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
                  {result.checks.map((c) => (
                    <li key={c.name} className="mono" style={{ fontSize: 12 }}>
                      <span className={c.passed ? "" : "pill danger"}>
                        {c.passed ? "✓" : "✗"}
                      </span>{" "}
                      {c.name}
                      {c.detail ? <div className="muted">{c.detail}</div> : null}
                    </li>
                  ))}
                </ul>
              </details>
            </>
          ) : <span className="muted">—</span>}
      </td>

      <td style={{ verticalAlign: "top" }}>
        <button onClick={onRun} disabled={disabled || running}>Run</button>
      </td>
    </tr>
  );
}
