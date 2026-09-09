import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ProviderHealth } from "../api/types";
import {
  Empty, ErrorBanner, SectionHead, Skeleton, StatStrip,
} from "../components/Bits";

/** How the provider has actually behaved — §26.
 *
 *  `/readiness` answers "is it reachable right now", which is the question a
 *  load balancer asks. An operator deciding whether to keep acting asks a
 *  different one: how has it been, on which operations, and is what I am seeing
 *  now unusual. One verdict cannot answer that.
 *
 *  ## UNKNOWN gets its own column and its own colour
 *
 *  An outcome nobody established is not a failure. A success rate written as
 *  `succeeded / attempted` quietly calls it one, and this page refuses to
 *  compute that number at all — three counts, and the reader decides. That is
 *  the same discipline the rest of the system applies to a stuck refund.
 *
 *  ## Latency is theirs, not ours
 *
 *  `provider_latency_ms` is time inside the call. Verification time is
 *  separate and excluded, because mixing them makes our own sweep look like
 *  their slowness.
 */
export default function Provider() {
  const [data, setData] = useState<ProviderHealth | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [days, setDays] = useState(14);

  useEffect(() => {
    let live = true;
    api.providerHealth(days).then(
      (d) => { if (live) { setData(d); setError(null); } },
      (e) => { if (live) setError(e as ApiError); },
    );
    return () => { live = false; };
  }, [days]);

  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!data) {
    return <div className="card"><SectionHead title="Provider" /><Skeleton rows={4} /></div>;
  }

  const totals = data.operations.reduce(
    (a, o) => ({
      attempted: a.attempted + o.attempted,
      succeeded: a.succeeded + o.succeeded,
      failed: a.failed + o.failed,
      unknown: a.unknown + o.unknown,
    }),
    { attempted: 0, succeeded: 0, failed: 0, unknown: 0 });

  const peak = Math.max(1, ...data.history.map((d) => d.attempted));

  return (
    <>
      <div className="card">
        <SectionHead title="Provider" count={data.environment}>
          <div className="filters" style={{ margin: 0 }}>
            {[7, 14, 30].map((d) => (
              <button key={d} type="button" aria-pressed={days === d}
                      onClick={() => setDays(d)}>
                {d} days
              </button>
            ))}
          </div>
        </SectionHead>
        <p className="sub">{data.detail}</p>
        {/* Said plainly rather than implied by a green light. A mock adapter is
            not a provider that answered. */}
        {!data.execution_is_real ? (
          <p className="sub">
            <span className="pill warn">execution is mocked</span>{" "}
            Nothing on this page reached a real payment provider. The policy,
            approval, idempotency and verification path is the same; only the
            outbound call differs.
          </p>
        ) : null}
        <StatStrip items={[
          ["Attempted", totals.attempted],
          ["Succeeded", totals.succeeded],
          ["Failed", totals.failed],
          // Its own figure. Never added to failure, never subtracted from
          // success — the whole point of the UNKNOWN state.
          ["Unknown", totals.unknown],
          ["Webhooks in", data.webhooks_received],
          ["Rejected", data.webhooks_rejected],
        ]} />
      </div>

      <div className="card">
        <SectionHead title="By operation" count={data.operations.length} />
        <p className="sub">
          A provider can be fine on one call and not another, and an overall
          rate hides exactly that.
        </p>
        {data.operations.length === 0 ? (
          <Empty>No provider calls in this window.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Operation</th><th className="num">Attempted</th>
                  <th className="num">Succeeded</th><th className="num">Failed</th>
                  <th className="num">Unknown</th>
                  <th className="num">p50</th><th className="num">p95</th>
                </tr>
              </thead>
              <tbody>
                {data.operations.map((o) => (
                  <tr key={o.action_type}>
                    <td><code className="perm">{o.action_type}</code></td>
                    <td className="num">{o.attempted}</td>
                    <td className="num">{o.succeeded}</td>
                    <td className="num">{o.failed || <span className="muted">0</span>}</td>
                    <td className="num">
                      {o.unknown
                        ? <span className="pill unknown">{o.unknown}</span>
                        : <span className="muted">0</span>}
                    </td>
                    <td className="num">
                      {o.p50_latency_ms == null
                        ? <span className="muted">—</span>
                        : `${o.p50_latency_ms} ms`}
                    </td>
                    <td className="num">
                      {o.p95_latency_ms == null
                        ? <span className="muted">—</span>
                        : `${o.p95_latency_ms} ms`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <SectionHead title="Day by day" count={`${days} days`} />
        <p className="sub">
          Whether today is unusual, which is the question a single verdict
          cannot answer.
        </p>
        {data.history.length === 0 ? (
          <Empty>Nothing happened in this window.</Empty>
        ) : (
          <ol className="prov-history" aria-label="Daily provider outcomes">
            {data.history.map((d) => (
              <li key={d.day}>
                <span className="prov-day mono">{d.day}</span>
                {/* Stacked in one track, scaled to the busiest day, so a quiet
                    day reads as quiet rather than as a full bar of nothing. */}
                <span className="prov-track" aria-hidden="true">
                  <span className="seg ok"
                        style={{ width: `${(d.succeeded / peak) * 100}%` }} />
                  <span className="seg bad"
                        style={{ width: `${(d.failed / peak) * 100}%` }} />
                  <span className="seg unk"
                        style={{ width: `${(d.unknown / peak) * 100}%` }} />
                </span>
                <span className="prov-counts muted">
                  {d.succeeded} ok · {d.failed} failed · {d.unknown} unknown
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </>
  );
}
