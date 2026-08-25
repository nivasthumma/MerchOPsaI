import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Scenario, ScenarioResult } from "../api/types";
import { Busy, ErrorBanner } from "../components/Bits";

export default function Scenarios() {
  const [all, setAll] = useState<Scenario[]>([]);
  const [category, setCategory] = useState("all");
  const [criticalOnly, setCriticalOnly] = useState(false);
  const [results, setResults] = useState<Record<string, ScenarioResult>>({});
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.scenarios().then(setAll).catch((e) => setError(e as ApiError));
  }, []);

  const categories = useMemo(
    () => ["all", ...Array.from(new Set(all.map((s) => s.category))).sort()], [all]);

  const shown = all.filter(
    (s) => (category === "all" || s.category === category) && (!criticalOnly || s.critical));

  async function run(id: string) {
    setRunning(id);
    setError(null);
    try {
      const result = await api.runScenario(id);
      setResults((r) => ({ ...r, [id]: result }));
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setRunning(null);
    }
  }

  return (
    <>
      <div className="card">
        <h2>Evaluation scenarios</h2>
        <p className="sub">
          {all.length} scenarios grading <strong>observable behaviour</strong> — tool
          sequence, policy decision, final status, verification state, whether an external
          financial effect occurred. Never prose. Each runs against a freshly seeded
          database.
        </p>
        <div className="row">
          <select value={category} onChange={(e) => setCategory(e.target.value)}
                  style={{ width: "auto" }}>
            {categories.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
          </select>
          <label style={{ margin: 0 }}>
            <input type="checkbox" checked={criticalOnly}
                   onChange={(e) => setCriticalOnly(e.target.checked)} /> critical only
          </label>
          <span className="muted">{shown.length} shown</span>
        </div>
      </div>

      <ErrorBanner error={error} />

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>ID</th><th>Category</th><th>Description</th><th>Result</th><th /></tr>
            </thead>
            <tbody>
              {shown.map((s) => {
                const r = results[s.id];
                return (
                  <tr key={s.id}>
                    <td className="mono">
                      {s.id} {s.critical ? <span className="pill danger">critical</span> : null}
                    </td>
                    <td className="muted">{s.category.replace(/_/g, " ")}</td>
                    <td style={{ maxWidth: 460 }}>{s.description}</td>
                    <td>
                      {running === s.id ? <Busy>running</Busy>
                        : r ? <span className={`pill ${r.passed ? "ok" : "danger"}`}>
                                {r.passed ? "pass" : "fail"}
                              </span>
                        : <span className="muted">—</span>}
                      {r && !r.passed ? (
                        <details>
                          <summary>failed checks</summary>
                          <ul>
                            {r.checks.filter((c) => !c.passed).map((c) => (
                              <li key={c.name} className="mono">{c.name}: {c.detail}</li>
                            ))}
                          </ul>
                        </details>
                      ) : null}
                    </td>
                    <td>
                      <button onClick={() => run(s.id)} disabled={!!running}>Run</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
