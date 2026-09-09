import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { PolicyControl, PolicyView } from "../api/types";
import {
  Busy, ErrorBanner, Money, SectionHead, Skeleton,
} from "../components/Bits";
import { useToast } from "../components/Toast";

/** What policy decides here — §41.
 *
 *  The engine's decisions have always been visible: an operator sees why a
 *  refund was held. What was missing is the rules themselves, before one fires.
 *
 *  ## Every control, not only the editable ones
 *
 *  Most of policy is deliberately not per-merchant — dual approval on high risk,
 *  risk computed rather than declared, the approval TTL. Showing only the one
 *  knob a merchant can turn would imply that is all policy is, which is the
 *  opposite of what this page is for. The uneditable ones are listed with the
 *  reason they are uneditable.
 *
 *  ## And one that is stored and does nothing
 *
 *  `auto_approve_below_minor` sits in every merchant's config and is read by no
 *  code path. It is shown, marked NOT IMPLEMENTED, because omitting it is how it
 *  stayed invisible: somebody setting it would believe small refunds
 *  auto-approve, and nothing would happen.
 */
export default function Policy() {
  const [data, setData] = useState<PolicyView | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function load() {
    try {
      const p = await api.policy();
      setData(p);
      setError(null);
      const limit = p.controls.find((c) => c.key === "refund_limit_minor");
      setDraft(limit ? String(Number(limit.effective) / 100) : "");
    } catch (e) {
      setError(e as ApiError);
    }
  }

  useEffect(() => { void load(); }, []);

  async function save(clear: boolean) {
    setBusy(true);
    setRefusal(null);
    try {
      const minor = clear ? null : Math.round(Number(draft) * 100);
      const change = await api.setRefundLimit(minor);
      toast({
        tone: "warn",
        title: change.changed
          ? (clear ? "Back to the platform default" : "Refund limit changed")
          : "No change",
        body: change.changed
          ? "This is what the engine reads on the next refund."
          : "The limit was already that.",
      });
      await load();
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 400 || err.status === 409) setRefusal(err);
      else setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="Policy" />
        <p className="sub">
          Policy requires the <span className="mono">owner</span> role. The
          refund limit is the largest amount this system will move on one
          instruction, so changing it is owner-only on purpose.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!data) {
    return <div className="card"><SectionHead title="Policy" /><Skeleton rows={4} /></div>;
  }

  const limit = data.controls.find((c) => c.key === "refund_limit_minor");

  return (
    <>
      <div className="card">
        <SectionHead title="Policy" count={data.merchant_id} />
        <p className="sub">
          What the engine decides for this merchant. Most of it is not
          configurable, and that is listed here too — a page showing only what
          you can change would imply that is all policy is.
        </p>
        {refusal ? <ErrorBanner error={refusal} /> : null}
      </div>

      {limit ? (
        <div className="card">
          <SectionHead title="Refund limit">
            {limit.overridden
              ? <span className="pill warn">overridden</span>
              : <span className="pill neutral">platform default</span>}
          </SectionHead>
          <p className="sub">{limit.why}</p>
          <dl className="kv">
            <dt>In force</dt>
            <dd><Money minor={Number(limit.effective)} /></dd>
            <dt>Platform default</dt>
            <dd><Money minor={Number(limit.default)} /></dd>
          </dl>

          <form className="row-form"
                onSubmit={(e) => { e.preventDefault(); void save(false); }}>
            <label>
              <span>New limit (₹)</span>
              <input type="number" min="0" step="0.01" value={draft} required
                     onChange={(e) => setDraft(e.target.value)} />
            </label>
            <button type="submit" className="primary" disabled={busy}>
              {busy ? <Busy>saving</Busy> : "Change limit"}
            </button>
            {/* Its own control, because clearing is not "set it to zero".
                Zero is a real limit that refuses every refund, and producing it
                by accident would be a quiet outage. */}
            <button type="button" disabled={busy || !limit.overridden}
                    title={limit.overridden ? undefined : "Already the default"}
                    onClick={() => void save(true)}>
              Use the platform default
            </button>
          </form>
        </div>
      ) : null}

      <div className="card">
        <SectionHead title="Not configurable here"
                     count={data.controls.filter((c) => !c.editable).length} />
        <p className="sub">
          Listed rather than omitted. Each of these is a decision somebody made
          about what a merchant should not be able to switch off.
        </p>
        <ul className="perm-grid">
          {data.controls.filter((c) => !c.editable).map((c) => (
            <li key={c.key} className={dead(c) ? "perm-row act" : "perm-row"}>
              <label>
                <code className={dead(c) ? "perm act" : "perm"}>{c.label}</code>
              </label>
              <span className="muted">
                {dead(c) ? <strong>Stored and ignored. </strong> : null}
                {c.why}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

/** A control that exists in the data and changes nothing. Worth the money
 *  colour: believing it works is worse than knowing it does not exist. */
function dead(c: PolicyControl): boolean {
  return c.why.startsWith("NOT IMPLEMENTED");
}
