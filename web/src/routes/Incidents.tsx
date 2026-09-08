import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { IncidentList, IncidentQuery, SavedView } from "../api/types";
import {
  Busy, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";
import { LiveBar } from "../components/LiveBar";
import { useToast } from "../components/Toast";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

/** MerchantOps §13 / §50.
 *
 *  The dashboard already linked to individual incidents and there was no page
 *  that listed them, so the only route into an incident was a table cell on
 *  another screen. An operations console whose work queue is a column of
 *  somebody else's report is not a queue.
 *
 *  Ordered by revenue at risk: the biggest problem is the one to open first.
 */
export default function Incidents() {
  const [params, setParams] = useSearchParams();
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  // Every filter lives in the URL — plan P1-05's saved views are only useful if
  // a filtered console is a link somebody can send. The query is derived from
  // the URL rather than held in component state for the same reason: two
  // sources for "what am I looking at" is how a shared link opens onto a
  // different list from the one that was shared.
  const query = useMemo<IncidentQuery>(() => {
    const list = (k: string) => params.getAll(k);
    const flag = (k: string) => params.get(k) === "true";
    const num = (k: string) => {
      const v = params.get(k);
      return v == null || v === "" ? undefined : Number(v);
    };
    return {
      view: params.get("view") ?? undefined,
      severity: list("severity"), status: list("status"),
      incident_type: list("incident_type"), payment_method: list("payment_method"),
      min_amount_minor: num("min_amount_minor"), max_age_hours: num("max_age_hours"),
      unresolved: flag("unresolved"), approval_required: flag("approval_required"),
      has_unknown: flag("has_unknown"), escalated: flag("escalated"),
      include_closed: flag("include_closed"),
    };
  }, [params]);

  // The plan's cadence for incidents is 5 seconds. `deps` is the serialised
  // query, not the object: a fresh object on every render would refetch forever.
  const key = params.toString();
  const live = useLiveRefresh<IncidentList>(
    () => api.incidents(query), { intervalMs: 5000, deps: [key] });
  const rows = live.data?.incidents ?? null;
  const atRisk = live.data?.total_revenue_at_risk_minor ?? 0;
  const views = live.data?.views ?? [];
  const matched = live.data?.matched ?? rows?.length ?? 0;
  const shown = live.data?.shown ?? rows?.length ?? 0;
  const load = live.refresh;

  /** Replace, never push: filtering is not navigation, and a back button that
   *  walks through twelve filter states is a back button nobody can use. */
  const setFilter = (next: URLSearchParams) => setParams(next, { replace: true });

  const applyView = (viewKey: string) => {
    const next = new URLSearchParams();
    // A view REPLACES the filter rather than merging into it. Merged, a chip
    // left set from earlier would silently narrow the view, and it would no
    // longer be the thing its label names.
    if (query.view !== viewKey) next.set("view", viewKey);
    setFilter(next);
  };

  const toggleFlag = (k: string) => {
    const next = new URLSearchParams(params);
    next.delete("view");        // a hand-set filter is no longer a saved view
    if (next.get(k) === "true") next.delete(k); else next.set(k, "true");
    setFilter(next);
  };

  const toggleValue = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    next.delete("view");
    const had = next.getAll(k);
    next.delete(k);
    for (const x of had) if (x !== v) next.append(k, x);
    if (!had.includes(v)) next.append(k, v);
    setFilter(next);
  };

  const filtered = key.length > 0;

  async function runDetection() {
    setBusy(true);
    try {
      const r = await api.detect();
      // Idempotent by construction, and saying so matters: an operator who
      // presses this twice should be told nothing new appeared rather than
      // left wondering whether it did.
      toast(r.incidents_created
        ? { tone: "ok", title: `Detected ${r.incidents_created} new incident(s)`,
            body: `${r.anomalies_found} anomalies found in ${r.duration_ms} ms.` }
        : { tone: "ok", title: "Nothing new",
            body: `${r.already_known} anomaly/anomalies already known. Detection is `
                  + `idempotent — a second sweep over the same window raises nothing.` });
      await load();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (error) return <ErrorBanner error={error} />;
  if (!rows) return <Skeleton rows={5} />;

  return (
    <div className="incidents">
      <SectionHead title="Incidents" count={rows.length}>
        <button onClick={runDetection} disabled={busy}>
          {busy ? <Busy>detecting</Busy> : "Run detection"}
        </button>
      </SectionHead>
      <LiveBar live={live} what="incidents" />

      <Views views={views} applied={query.view} onApply={applyView} />
      <Filters query={query} onFlag={toggleFlag} onValue={toggleValue}
               onClear={() => setFilter(new URLSearchParams())}
               filtered={filtered} />

      <StatStrip items={[
        // The server's count over the whole match, not `rows.length`, which is
        // the size of this page.
        [filtered ? "Matching" : "Open", matched],
        ["Revenue at risk", <Money key="r" minor={atRisk} />],
      ]} />

      {matched > shown ? (
        <p className="sub">
          <strong>Showing {shown} of {matched}.</strong> The revenue figure
          above covers all {matched} — it is counted in SQL, not summed across
          this page.
        </p>
      ) : null}

      {rows.length === 0 ? (
        <Empty>
          {filtered
            ? "No incident matches this filter. The filter is applied server-side, "
              + "so this is the whole answer and not a page of it."
            : "No open incidents. Detection is a sweep, not a daemon — run it to "
              + "look again."}
        </Empty>
      ) : (
        <table className="grid" aria-label="Open incidents">
          <thead>
            <tr>
              <th>Incident</th><th>Type</th><th>Severity</th><th>Status</th>
              <th className="num">At risk</th><th>Started</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i.id}>
                <td><Link to={`/incidents/${i.id}`}>{i.title}</Link></td>
                <td>{i.type.toLowerCase().replace(/_/g, " ")}</td>
                <td data-sev={i.severity}>{i.severity}</td>
                <td>{i.status.toLowerCase().replace(/_/g, " ")}</td>
                <td className="num"><Money minor={i.revenue_at_risk_minor} /></td>
                <td><When iso={i.started_at} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


/** The five saved views — plan P1-05.
 *
 *  Labels, hints and counts all come from the server. A client that held its
 *  own copy of what "My attention" means would be a second definition, and the
 *  one in a pasted link would win on some screens and lose on others. */
function Views({ views, applied, onApply }: {
  views: SavedView[]; applied?: string; onApply: (key: string) => void;
}) {
  if (views.length === 0) return null;
  return (
    <nav className="chips" aria-label="Saved views">
      {views.map((v) => (
        // The accessible name says which control this is, because "UNKNOWN"
        // names both a saved view and a severity-adjacent filter chip below.
        // Two buttons that sound identical and do different things is a
        // control a screen-reader user cannot choose between (P1-12).
        <button key={v.key} className={`chip ${applied === v.key ? "info" : ""}`}
                aria-pressed={applied === v.key}
                aria-label={`Saved view: ${v.label} (${v.count})`}
                title={v.hint} onClick={() => onApply(v.key)}>
          {v.label} <b>{v.count}</b>
        </button>
      ))}
    </nav>
  );
}

/** The filter chips.
 *
 *  Only the ones an operator reaches for under pressure are surfaced as chips;
 *  the rest of P1-05's eleven are reachable by URL and are used by the saved
 *  views. A wall of eleven controls is a wall, not a filter. */
function Filters({ query, onFlag, onValue, onClear, filtered }: {
  query: IncidentQuery;
  onFlag: (k: string) => void;
  onValue: (k: string, v: string) => void;
  onClear: () => void;
  filtered: boolean;
}) {
  const flags: [string, string, string][] = [
    ["unresolved", "Unresolved", "Still open — not resolved, closed or cancelled."],
    ["approval_required", "Awaiting approval",
     "A person is the gate. Nothing has reached the provider."],
    ["has_unknown", "UNKNOWN",
     "An action whose outcome could not be established. Being reconciled."],
    ["escalated", "Escalated",
     "Automatic reconciliation is exhausted. A person owns it."],
  ];

  return (
    <div className="filters" role="group" aria-label="Filters">
      {flags.map(([k, label, hint]) => (
        <button key={k} aria-pressed={Boolean((query as Record<string, unknown>)[k])}
                title={hint} onClick={() => onFlag(k)}>
          {label}
        </button>
      ))}

      <span className="filter-sep" aria-hidden="true" />

      {["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((sev) => (
        <button key={sev} aria-pressed={query.severity?.includes(sev) ?? false}
                onClick={() => onValue("severity", sev)}>
          {sev}
        </button>
      ))}

      {filtered ? (
        <button className="linkish" onClick={onClear}>Clear</button>
      ) : null}
    </div>
  );
}
