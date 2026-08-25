import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api/client";
import type { Health, Principal } from "../api/types";
import { SectionHead } from "../components/Bits";
import { useToast } from "../components/Toast";

/** Settings.
 *
 *  The run configuration is *reported* in the app shell, where it cannot be
 *  missed. This page is where it is *changed* — a deliberate split: a warning
 *  belongs in front of you, a control belongs somewhere you went on purpose.
 */
export default function Settings() {
  const ctx = useOutletContext<
    { me: Principal | null; health: Health | null;
      onHealth: (h: Health) => void } | null>();
  const me = ctx?.me ?? null;
  const health = ctx?.health ?? null;

  if (!health) {
    return (
      <div className="card">
        <div className="banner danger">
          <strong>API unreachable.</strong> Settings read the server's own view of its
          configuration, so there is nothing trustworthy to show until it answers.
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <p className="request">
          What this server resolved at startup, and the one part of it you can change
          from here. Everything on this page is read from the API — the app never
          infers a setting it was not told.
        </p>
      </div>

      <div className="card">
        <SectionHead title="Reasoning provider" />
        <ProviderControl health={health} me={me} onChange={ctx!.onHealth} />
      </div>

      <div className="card">
        <SectionHead title="Resolved configuration" />
        <p className="sub">
          Reported by <code>/health</code>, which is unauthenticated on purpose so the
          run configuration is visible before anyone signs in.
        </p>
        <dl className="kv">
          <dt>Payment adapter</dt>
          {/* The pill says what the adapter means, not what it is called. It
              used to repeat the value — "mock (mock)" told nobody anything. */}
          <dd>{health.payment_adapter}{" "}
            {health.razorpay_execution_is_real
              ? <span className="pill warn">reaches Razorpay</span>
              : <span className="pill neutral">no outbound call</span>}</dd>
          <dt>Reasoning</dt><dd>{health.llm_model}</dd>
          <dt>Provider</dt>
          <dd>{health.llm_provider} <span className="muted">({health.llm_provider_source})</span></dd>
          <dt>Credential</dt>
          <dd>{health.llm_credential_source ?? <span className="muted">none detected</span>}</dd>
          <dt>Auth</dt><dd>{health.auth}</dd>
          <dt>Signing secret</dt>
          <dd>{health.auth_secret_is_development_default
            ? <span className="pill danger">development default</span>
            : <span className="pill ok">configured</span>}</dd>
        </dl>
      </div>

      <div className="card">
        <SectionHead title="This browser" />
        <p className="sub">
          Theme and density live in the header. Both are stored per browser and never
          sent to the server; neither hides a state, a label, or a warning.
        </p>
        <p className="sub">
          Your bearer token is kept in this browser's local storage only. Signing out
          removes it. It carries no permissions of its own — those are read from the
          database on every request, so a token cannot grant itself authority.
        </p>
      </div>
    </>
  );
}

/** Selects among providers the server can already reach.
 *
 * There is no field for a key here, and there will not be: CONTRACT §37 keeps
 * provider secrets in the environment, and a browser form is neither an
 * environment variable nor an appropriate secret mechanism. If no credential is
 * configured, the server refuses the switch and says how to configure one.
 */
function ProviderControl(
  { health, me, onChange }:
  { health: Health; me: Principal | null; onChange: (h: Health) => void },
) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const toast = useToast();
  const isOwner = me?.role === "owner";

  async function choose(provider: "auto" | "deterministic" | "anthropic") {
    setBusy(provider);
    setProblem(null);
    try {
      const r = await api.setProvider(provider);
      onChange({ ...health, llm_provider: r.llm_provider, llm_model: r.llm_model,
                 llm_provider_source: r.llm_provider_source });
      toast({ tone: "ok", title: `Reasoning now: ${r.llm_provider}`,
              body: r.changed_from === r.llm_provider
                ? "Unchanged." : `Was ${r.changed_from}. This process only.` });
    } catch (e) {
      const err = e as { message?: string };
      setProblem(err.message ?? "The switch was refused.");
    } finally {
      setBusy(null);
    }
  }

  if (!isOwner) {
    return (
      <p className="sub" style={{ marginTop: 4 }}>
        Reasoning provider: <code>{health.llm_provider}</code> (
        {health.llm_provider_source}). Changing it requires the owner role.
      </p>
    );
  }

  return (
    <div style={{ marginTop: 4 }}>
      <p className="sub">
        Selects between providers this server can already reach. Credentials stay in the
        server environment — there is no field for a key here, and adding one would put a
        provider secret in a browser. A switch applies to this process only and does not
        survive a restart.
      </p>
      <div className="filters">
        {(["auto", "deterministic", "anthropic"] as const).map((p) => (
          <button key={p} disabled={!!busy}
                  aria-pressed={health.llm_provider_source === "runtime"
                    ? health.llm_provider === p
                    : p === "auto" && health.llm_provider_source === "auto"}
                  onClick={() => choose(p)}>
            {busy === p ? "…" : p}
          </button>
        ))}
      </div>
      {problem ? <div className="banner warn" style={{ marginTop: 10 }}>{problem}</div> : null}
      {health.llm_provider !== "deterministic" ? (
        <div className="banner warn" style={{ marginTop: 10 }}>
          <strong>Published metrics were measured on the deterministic planner.</strong>{" "}
          Scenario runs and evaluation numbers produced under a language model measure
          something different and should be reported separately.
        </div>
      ) : null}
    </div>
  );
}
