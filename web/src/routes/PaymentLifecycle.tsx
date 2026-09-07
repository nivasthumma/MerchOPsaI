// One payment, end to end — MerchantOps §7.
//
// The page an operator lands on with an identifier in the clipboard and a
// customer on the phone. It answers one question — *what happened to this
// payment* — and the answer used to require four screens, because a payment's
// life spans several operations with several correlation ids.
//
// Everything here is a row the server found. This page derives nothing: it
// does not infer a missing step, does not reorder events into the sequence
// they "should" have happened in, and does not summarise. An out-of-order
// provider event is shown out of order, because that is the fact.

import { Link, useParams } from "react-router";
import { api } from "../api/client";
import type { LifecycleEvent, PaymentLifecycle as Data } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, Money, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";
import { LiveBar } from "../components/LiveBar";
import { Status } from "../components/Status";
import { useLiveRefresh } from "../hooks/useLiveRefresh";

const INTERVAL_MS = 8000;

// A shape per stage, so the chain is readable in greyscale and by anyone who
// cannot rely on colour (P1-12). Stage names come from the server; an
// unrecognised one still renders, with a neutral mark rather than a guess.
const MARK: Record<string, string> = {
  payment: "•",
  mapping: "⇄",
  incident: "▲",
  investigation: "◎",
  tool_call: "·",
  approval: "⏸",
  action: "▶",
  provider_event: "⇠",
  verification: "✓",
  refund: "↩",
};

export default function PaymentLifecycle() {
  const { paymentId = "" } = useParams();
  const live = useLiveRefresh<Data>(
    () => api.paymentLifecycle(paymentId),
    { intervalMs: INTERVAL_MS, deps: [paymentId] });
  const d = live.data;

  if (live.error && !d) return <ErrorBanner error={live.error} />;
  if (!d) return <Skeleton rows={6} />;

  const p = d.payment;

  return (
    <div className="lifecycle">
      <SectionHead title={`Payment ${p.id}`}>
        <Status status={p.status.toUpperCase()} compact />
      </SectionHead>
      <LiveBar live={live} what="this payment" />

      <section className="card">
        <h3 className="card-title">The payment</h3>
        <StatStrip items={[
          ["Amount", <Money key="a" minor={p.amount_minor} />],
          ["Method", p.method],
          ["Refunded", <Money key="r" minor={p.amount_refunded_minor} />],
          ["Created", <When key="c" iso={p.created_at ?? ""} />],
        ]} />

        <dl className="kv">
          <dt>Customer</dt>
          <dd>
            {p.customer_name ?? "—"}{" "}
            {p.customer_id ? <span className="muted">{p.customer_id}</span> : null}
          </dd>
          <dt>Order</dt>
          <dd>{p.order_id ?? "—"}</dd>
          <dt>Provider</dt>
          <dd>
            {d.external_payment_id ? (
              <><CopyId value={d.external_payment_id} />{" "}
                <span className="muted">{d.provider} · {d.environment}</span></>
            ) : (
              // The rejection path working, not a defect — and the page says
              // which, because "—" reads as missing data.
              <span className="muted">
                not mapped · this payment cannot be executed against externally
              </span>
            )}
          </dd>
          {p.error_reason ? (
            <>
              <dt>Failure reason</dt>
              <dd>{p.error_reason}</dd>
            </>
          ) : null}
        </dl>
      </section>

      <section className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
          <h3 className="card-title" style={{ margin: 0 }}>
            Lifecycle <span className="count">{d.events.length}</span>
          </h3>
        </div>
        <p className="sub">
          Every entry is a row this system wrote, in the order it happened —
          not the order the stages usually occur in. A provider event that
          arrived late is shown late, because that is the fact worth seeing.
        </p>

        {d.events.length === 0 ? (
          <Empty>Nothing has touched this payment yet.</Empty>
        ) : (
          <ol className="chain" aria-label="Payment lifecycle">
            {d.events.map((e, i) => (
              <Event key={`${e.stage}-${e.id}-${i}`} e={e} />
            ))}
          </ol>
        )}
      </section>

      <section className="card">
        <h3 className="card-title">Where else this appears</h3>
        <p className="sub">
          {/* The count is the point: more than one correlation id is exactly
              why this page exists rather than a link to /trace. */}
          This payment's story spans <strong>{d.correlation_ids.length}</strong>{" "}
          correlated operation{d.correlation_ids.length === 1 ? "" : "s"}.
          {d.correlation_ids.length > 1
            ? " No single trace covers it, which is why they are listed separately."
            : null}
        </p>
        <dl className="kv">
          <dt>Incidents</dt>
          <dd>
            {d.incident_ids.length === 0 ? <span className="muted">none</span>
              : d.incident_ids.map((id) => (
                  <Link key={id} to={`/incidents/${id}`} className="mono"
                        style={{ marginRight: 8 }}>{id}</Link>))}
          </dd>
          <dt>Investigations</dt>
          <dd>
            {d.task_ids.length === 0 ? <span className="muted">none</span>
              : d.task_ids.map((id) => (
                  <Link key={id} to={`/tasks/${id}`} className="mono"
                        style={{ marginRight: 8 }}>{id}</Link>))}
          </dd>
          <dt>Actions</dt>
          <dd>
            {d.action_ids.length === 0 ? <span className="muted">none</span>
              : <Link to="/actions" className="mono">
                  {d.action_ids.join(", ")}
                </Link>}
          </dd>
          <dt>Correlations</dt>
          <dd className="mono">
            {d.correlation_ids.length === 0 ? <span className="muted">none</span>
              : d.correlation_ids.join(", ")}
          </dd>
        </dl>
      </section>
    </div>
  );
}

function Event({ e }: { e: LifecycleEvent }) {
  return (
    <li data-stage={e.stage}>
      <span className="chain-mark" aria-hidden="true">{MARK[e.stage] ?? "·"}</span>
      <span className="chain-body">
        <span className="chain-label">
          {e.label}
          {/* The stage name is the machine's word for this step and stays
              available; the label is the operator's. */}
          <span className="sr-only"> ({e.stage.replace(/_/g, " ")})</span>
        </span>
        {e.detail ? <span className="chain-detail muted">{e.detail}</span> : null}
      </span>
      <span className="chain-when mono">
        {e.at
          ? new Date(e.at).toLocaleString()
          : <span className="muted" title="This step is derived from a state rather than an event, so it has no honest timestamp.">—</span>}
      </span>
    </li>
  );
}
