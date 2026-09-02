import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { LiveEvent } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";

/** The live timeline — MerchantOps v2 §62, §65.
 *
 *  §65 draws a run as it happens:
 *
 *      18:07:20 Detection triggered
 *      18:07:22 Incident created
 *      18:07:24 Agent started
 *      ...
 *
 *  ## Appended, never re-fetched
 *
 *  Each poll asks for what came AFTER the last id this page saw, and the
 *  results are appended. Re-fetching the whole list would make the timeline
 *  flicker and would quietly rewrite history if an earlier frame were ever
 *  edited — and a timeline that can change what it already said is not a
 *  record of anything.
 *
 *  ## `pending` is on screen for a reason
 *
 *  The server writes frames into an outbox and a drain delivers them. If the
 *  drain stops, the timeline simply stops moving, which looks exactly like a
 *  quiet system. `pending` is the difference between "nothing is happening"
 *  and "nothing is being delivered", and it is the only way to tell them apart
 *  from here. */

/** §62's fifteen, grouped for colour. Anything unrecognised renders as itself
 *  rather than being dropped: the vocabulary is closed server-side, so an
 *  unfamiliar name means this app is behind, and hiding it would hide that. */
const TONE: Record<string, string> = {
  "incident.created": "warn",
  "incident.resolved": "ok",
  "agent.started": "info",
  "tool.started": "info",
  "tool.completed": "info",
  "evidence.discovered": "info",
  "hypothesis.created": "info",
  "hypothesis.rejected": "muted",
  "recommendation.created": "info",
  "policy.evaluated": "info",
  "approval.requested": "warn",
  "action.started": "warn",
  "action.completed": "ok",
  "verification.started": "info",
  "verification.completed": "ok",
};

/** The one line §65 shows per frame. Built from the payload the server sent,
 *  never from a template that assumes fields exist. */
function summarise(e: LiveEvent): string {
  const p = e.payload ?? {};
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = p[k];
      if (v !== undefined && v !== null && v !== "") return String(v);
    }
    return "";
  };
  switch (e.event) {
    case "incident.created":
      return pick("incident_type", "rule") || "an incident was raised";
    case "hypothesis.created":
    case "hypothesis.rejected":
      return [pick("label"), pick("key")].filter(Boolean).join(" ") || "hypothesis";
    case "policy.evaluated":
      return pick("decision", "rule") || "policy evaluated";
    case "tool.completed":
      return pick("tool", "tool_name") || "a tool returned";
    case "action.started":
    case "action.completed":
      return pick("action_type", "status") || "action";
    case "verification.completed":
      return pick("state", "detail") || "verified";
    default:
      return pick("reason", "detail", "status", "intent");
  }
}

export default function Timeline() {
  const [events, setEvents] = useState<LiveEvent[] | null>(null);
  const [pending, setPending] = useState(0);
  const [live, setLive] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [params, setParams] = useSearchParams();
  const incidentFilter = params.get("incident") ?? "";

  // Held in a ref rather than state: the poll reads it on every tick, and
  // putting it in the dependency array would tear the interval down and build
  // a new one after every frame that arrived.
  const cursor = useRef<string | null>(null);

  const poll = useCallback(async () => {
    try {
      const page = await api.events(cursor.current);
      setPending(page.pending);
      setError(null);
      if (page.events.length) {
        cursor.current = page.next_cursor ?? cursor.current;
        // Newest first, so the thing that just happened is where the eye is.
        setEvents((prev) => [...page.events].reverse().concat(prev ?? []));
      } else {
        setEvents((prev) => prev ?? []);
      }
    } catch (e) {
      setError(e as ApiError);
    }
  }, []);

  useEffect(() => { void poll(); }, [poll]);

  // Two seconds while the tab is visible, nothing while it is not. A timeline
  // is the one screen where a stale view is actively misleading, and a
  // background tab polling every two seconds is a cost nobody asked for.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => { if (!timer) timer = setInterval(() => void poll(), 2000); };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const onVisibility = () => {
      if (document.hidden) { setLive(false); stop(); }
      else { setLive(true); void poll(); start(); }
    };
    setLive(!document.hidden);
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [poll]);

  const shown = incidentFilter
    ? (events ?? []).filter((e) => e.incident_id === incidentFilter)
    : (events ?? []);

  const setIncident = (id: string) => {
    const next = new URLSearchParams(params);
    if (id) next.set("incident", id); else next.delete("incident");
    setParams(next, { replace: true });
  };

  return (
    <section>
      <SectionHead title="Live timeline" count={shown.length || null}>
        <span className="sub">
          MerchantOps §62 — what the system is doing, as it does it.
        </span>
      </SectionHead>

      <StatStrip items={[
        ["Frames", String(shown.length)],
        // Undelivered, not unseen. A number that only grows means the drain
        // has stopped; the timeline going quiet looks identical otherwise.
        ["Undelivered", pending > 0
          ? <span className="warn" title="Frames written but not yet delivered to consumers.">{pending}</span>
          : "0"],
        ["Polling", live ? "every 2s" : "paused (tab hidden)"],
      ]} />

      {error && <ErrorBanner error={error} />}

      {incidentFilter && (
        <p className="filter">
          Showing <CopyId value={incidentFilter} label="incident" /> only.{" "}
          <button type="button" className="link" onClick={() => setIncident("")}>
            Show everything
          </button>
        </p>
      )}

      {events === null ? <Skeleton rows={6} /> : shown.length === 0 ? (
        <Empty>
          {pending > 0
            ? `Nothing delivered yet — ${pending} frame(s) are waiting on the drain.`
            : "No activity yet. Run a detection sweep or an investigation."}
        </Empty>
      ) : (
        <ol className="live-feed" aria-label="Live timeline">
          {shown.map((e) => (
            <li key={e.id} className={`feed-row feed-${TONE[e.event] ?? "muted"}`}>
              <time className="feed-at" dateTime={e.occurred_at}
                    title={new Date(e.occurred_at).toLocaleString()}>
                {new Date(e.occurred_at).toLocaleTimeString()}
              </time>
              <span className="feed-name">{e.event}</span>
              <span className="feed-what">{summarise(e)}</span>
              <span className="feed-links">
                {e.incident_id && (
                  <Link to={`/incidents/${encodeURIComponent(e.incident_id)}`}>
                    {e.incident_id}
                  </Link>
                )}
                {e.task_id && (
                  <Link to={`/tasks/${encodeURIComponent(e.task_id)}`}>task</Link>
                )}
                {e.incident_id && e.incident_id !== incidentFilter && (
                  <button type="button" className="link"
                          onClick={() => setIncident(e.incident_id as string)}>
                    only this
                  </button>
                )}
              </span>
              <span className="feed-ago"><When iso={e.occurred_at} /></span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
