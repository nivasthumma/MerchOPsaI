import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { api, getToken, setToken } from "./api/client";
import type { Health } from "./api/types";
import { ThemeToggle } from "./components/Theme";
import { ToastHost } from "./components/Toast";

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
    <ToastHost>
      <header className="top">
        <div className="top-inner">
          <div className="logo">
            <Mark />
            <div>
              <div className="name">MerchantOps Agent</div>
              <span className="kicker">control plane</span>
            </div>
          </div>
          <nav className="tabs">
            <NavLink to="/" end>Investigate</NavLink>
            <NavLink to="/scenarios">Scenarios</NavLink>
            <NavLink to="/operations">Operations</NavLink>
          </nav>
          <ThemeToggle />
          {token ? (
            <button onClick={() => { setToken(""); setTok(""); }}>Sign out</button>
          ) : null}
        </div>
      </header>

      <div className="shell">
        <RunConfig health={health} />
        {token ? <Outlet /> : <SignIn draft={draft} setDraft={setDraft} save={save} />}
      </div>
    </ToastHost>
  );
}

function Mark() {
  // A gate with something passing through it, which is what the project is.
  return (
    <svg width="30" height="30" viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect x="1" y="1" width="30" height="30" rx="9"
            fill="var(--accent-soft)" stroke="var(--accent-border)" />
      <path d="M8 21V11a4 4 0 0 1 8 0v10" stroke="var(--accent)" strokeWidth="2.1"
            strokeLinecap="round" />
      <path d="M16 16h8" stroke="var(--accent)" strokeWidth="2.1" strokeLinecap="round" />
      <circle cx="24" cy="16" r="2.4" fill="var(--accent)" />
    </svg>
  );
}

/** The run configuration, compressed to chips with the full disclosures one
 *  click away. The wording of those disclosures is unchanged: a demo running
 *  against a mock adapter has to say so in words, not only in a colour. */
function RunConfig({ health }: { health: Health | null }) {
  if (!health) {
    return (
      <div className="banner danger">
        <strong>API unreachable.</strong> Start it with <code>make api</code> — this app
        proxies <code>/api</code> to <code>127.0.0.1:8000</code>.
      </div>
    );
  }

  const real = health.razorpay_execution_is_real;
  const model = health.llm_provider !== "deterministic";
  const devSecret = health.auth_secret_is_development_default;

  return (
    <details className="config" open={devSecret}>
      <summary>
        <span className={`chip ${real ? "info" : "warn"}`}>
          <span className="dot" />
          {real ? "Razorpay Test Mode" : "Mock payment adapter"}
        </span>
        <span className={`chip ${model ? "info" : "warn"}`}>
          <span className="dot" />
          {model ? health.llm_model : "Deterministic planner"}
        </span>
        {devSecret ? (
          <span className="chip danger"><span className="dot" />Development signing secret</span>
        ) : (
          <span className="chip ok"><span className="dot" />Signed tokens</span>
        )}
        <span className="expand">run configuration ▾</span>
      </summary>

      <div className="config-body">
        {!real ? (
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

        {!model ? (
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

        {devSecret ? (
          <div className="banner danger">
            <strong>Development signing secret in use.</strong> Tokens minted here are
            forgeable by anyone with the source. Set <code>API_TOKEN_SECRET</code> before
            this is reachable by anyone else.
          </div>
        ) : null}
      </div>
    </details>
  );
}

function SignIn(
  { draft, setDraft, save }:
  { draft: string; setDraft: (s: string) => void; save: () => void },
) {
  return (
    <div className="card" style={{ maxWidth: 620 }}>
      <h2 style={{ marginTop: 0 }}>Bearer token</h2>
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
      <div className="row" style={{ marginTop: 14 }}>
        <button className="primary" onClick={save} disabled={!draft.trim()}>Use token</button>
        <span className="muted">Stored in this browser only, never sent anywhere but the API.</span>
      </div>
    </div>
  );
}
