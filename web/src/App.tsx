import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { api, getToken, setToken } from "./api/client";
import type { Health } from "./api/types";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [token, setTok] = useState(getToken());
  const [draft, setDraft] = useState("");

  useEffect(() => {
    // /health is unauthenticated on purpose, so the run configuration is
    // visible before anyone signs in. What it reports is exactly what the
    // backend resolved — this app never infers it.
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  function save() {
    setToken(draft.trim());
    setTok(draft.trim());
    setDraft("");
  }

  return (
    <>
      <header className="top">
        <div className="top-inner">
          <div className="brand">
            MerchantOps Agent
            <span>control plane</span>
          </div>
          <nav className="tabs">
            <NavLink to="/" end>Investigate</NavLink>
            <NavLink to="/scenarios">Scenarios</NavLink>
            <NavLink to="/operations">Operations</NavLink>
          </nav>
          {token ? (
            <button onClick={() => { setToken(""); setTok(""); }}>Sign out</button>
          ) : null}
        </div>
      </header>

      <div className="shell">
        <RunConfig health={health} />
        {token ? <Outlet /> : <SignIn draft={draft} setDraft={setDraft} save={save} />}
      </div>
    </>
  );
}

function RunConfig({ health }: { health: Health | null }) {
  if (!health) {
    return (
      <div className="banner danger">
        <strong>API unreachable.</strong> Start it with <code>make api</code> — this app
        proxies <code>/api</code> to <code>127.0.0.1:8000</code>.
      </div>
    );
  }
  return (
    <>
      {!health.razorpay_execution_is_real ? (
        <div className="banner warn">
          <strong>Refunds execute against a mock adapter</strong>, not Razorpay
          (<code>{health.payment_adapter}</code>). Policy, approval, idempotency and
          verification are identical on both paths — only the outbound call differs.
        </div>
      ) : (
        <div className="banner info">
          <strong>Live Razorpay Test Mode.</strong> Approved refunds hit the provider.
        </div>
      )}
      {health.llm_provider === "deterministic" ? (
        <div className="banner info">
          <strong>Reasoning is the deterministic planner</strong>, not a language model
          {health.llm_credential_source === null
            ? " (no Anthropic credential detected)"
            : ` (LLM_PROVIDER is set explicitly)`}
          . Results measure the control plane, not model intelligence.
        </div>
      ) : (
        <div className="banner info">
          Reasoning: <code>{health.llm_model}</code>, authenticated via{" "}
          <code>{health.llm_credential_source}</code>.
        </div>
      )}
      {health.auth_secret_is_development_default ? (
        <div className="banner danger">
          <strong>Development signing secret in use.</strong> Tokens minted here are
          forgeable by anyone with the source. Set <code>API_TOKEN_SECRET</code> before
          this is reachable by anyone else.
        </div>
      ) : null}
    </>
  );
}

function SignIn(
  { draft, setDraft, save }:
  { draft: string; setDraft: (s: string) => void; save: () => void },
) {
  return (
    <div className="card" style={{ maxWidth: 620 }}>
      <h2>Bearer token</h2>
      <p className="sub">
        The token identifies you. It carries no permissions — those are read from the
        database on every request, so a token cannot grant itself authority.
      </p>
      <label htmlFor="tok">Mint one with <code>make token USER_ID=USR_A_OWNER</code></label>
      <input
        id="tok" type="password" value={draft} placeholder="paste token"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") save(); }}
      />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" onClick={save} disabled={!draft.trim()}>Use token</button>
        <span className="muted">Stored in this browser only, never sent anywhere but the API.</span>
      </div>
    </div>
  );
}
