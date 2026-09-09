import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, ApiError } from "../api/client";
import type { AuditEntry, AuditPage } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";

/** The auditor's question — §28.
 *
 *  The trail was already visible wherever it was relevant: on a task, on an
 *  incident, on the live timeline. What was missing is the question that is not
 *  about one object — "everything this person did", "every approval last
 *  quarter", "what happened in that window".
 *
 *  ## The count is of the filter, not of the page
 *
 *  `matched` comes from the server's COUNT over the same WHERE clause. Deriving
 *  a total from the rows on screen is a defect this repository has written
 *  three times, and in an audit context it is the worst place for it: a reader
 *  concludes there were four approvals because four fit on the page.
 *
 *  ## Older, not "page 2"
 *
 *  Keyset paging. `audit_logs` is append-only and receives writes while
 *  somebody reads it, so OFFSET would show a row twice or skip one — which here
 *  reads as an event that is missing rather than as a paging artefact.
 */
export default function Audit() {
  const [page, setPage] = useState<AuditPage | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [eventType, setEventType] = useState("");
  const [actor, setActor] = useState("");
  const [older, setOlder] = useState<AuditEntry[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    try {
      setOlder([]);
      setPage(await api.audit({
        eventType: eventType || undefined,
        actor: actor || undefined,
      }));
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }, [eventType, actor]);

  useEffect(() => { void load(); }, [load]);

  async function more() {
    if (!page) return;
    const cursor = older.length
      ? older[older.length - 1].id
      : page.next_cursor;
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const next = await api.audit({
        eventType: eventType || undefined,
        actor: actor || undefined,
        beforeId: cursor,
      });
      setOlder((prev) => [...prev, ...next.entries]);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setLoadingMore(false);
    }
  }

  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="Audit" />
        <p className="sub">
          Reading the trail requires a role with{" "}
          <span className="mono">read:orders</span>.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!page) {
    return <div className="card"><SectionHead title="Audit" /><Skeleton rows={5} /></div>;
  }

  const rows = [...page.entries, ...older];
  const exhausted = rows.length >= page.matched;

  return (
    <>
      <div className="card">
        <SectionHead title="Audit" count={`${page.matched} matching`} />
        <p className="sub">
          Append-only, enforced by a database trigger, and merchant-scoped in
          SQL rather than filtered afterwards. Nothing here can be edited or
          removed — including by retention, which leaves this table alone.
        </p>
        <StatStrip items={[
          // The filter's count, from the server. Never `rows.length`.
          ["Matching", page.matched],
          ["Showing", rows.length],
          ["Kinds of event", page.event_types.length],
        ]} />
      </div>

      <div className="card">
        <SectionHead title="Filter">
          <div className="filters" style={{ margin: 0 }}>
            <button type="button" aria-pressed={!eventType && !actor}
                    onClick={() => { setEventType(""); setActor(""); }}>
              Everything
            </button>
          </div>
        </SectionHead>
        <div className="row-form">
          <label>
            <span>What happened</span>
            {/* Options come from the data. A hardcoded list goes stale in the
                direction that hides events. */}
            <select value={eventType} onChange={(e) => setEventType(e.target.value)}>
              <option value="">any event</option>
              {page.event_types.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Who did it</span>
            <input value={actor} placeholder="USR_A_OWNER"
                   onChange={(e) => setActor(e.target.value)} />
          </label>
        </div>
      </div>

      <div className="card">
        <SectionHead title="Entries" count={`${rows.length} of ${page.matched}`} />
        {rows.length === 0 ? (
          <Empty>Nothing in the trail matches that.</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th><th>Event</th><th>Who</th>
                    <th>Concerning</th><th>Correlation</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => (
                    <tr key={e.id}>
                      <td><When iso={e.created_at} /></td>
                      <td><code className="perm">{e.event_type}</code></td>
                      <td className="mono">
                        {e.user_id ?? <span className="muted">system</span>}
                      </td>
                      <td className="mono">
                        {/* Links back into the object, so the trail is a way in
                            rather than a dead end. */}
                        {e.incident_id
                          ? <Link to={`/incidents/${e.incident_id}`}>{e.incident_id}</Link>
                          : e.task_id
                            ? <Link to={`/tasks/${e.task_id}`}>{e.task_id}</Link>
                            : <span className="muted">—</span>}
                      </td>
                      <td>
                        {e.correlation_id
                          ? <CopyId value={e.correlation_id} label="correlation id" />
                          : <span className="muted">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!exhausted ? (
              // "Older", not "next page": this walks backwards through an
              // append-only table, and page numbers would imply a fixed set.
              <button type="button" disabled={loadingMore} onClick={() => void more()}>
                {loadingMore ? "Loading…" : "Show older"}
              </button>
            ) : (
              <p className="sub">That is everything matching this filter.</p>
            )}
          </>
        )}
      </div>
    </>
  );
}
