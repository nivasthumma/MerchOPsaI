import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { api, getToken, isDemoSession, setToken } from "./api/client";
import type { Health, Metrics, Principal } from "./api/types";
import { ActivityBar, DensityToggle } from "./components/Chrome";
import { CommandPalette } from "./components/CommandPalette";
import { ThemeToggle } from "./components/Theme";
import { ToastHost } from "./components/Toast";
import { readRecent, subscribeRecent, forgetRecent, type RecentTask } from "./recent";

export default function App() {
  const location = useLocation();
  const [health, setHealth] = useState<Health | null>(null);
  const [me, setMe] = useState<Principal | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [token, setTok] = useState(getToken());
  const [draft, setDraft] = useState("");

  useEffect(() => {
    // /health is unauthenticated on purpose, so the run configuration is
    // visible before anyone signs in. What it reports is exactly what the
    // backend resolved — this app never infers it.
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  // Who the server thinks you are. The same screens behave differently for an
  // owner and an analyst, and nobody should have to infer which they are.
  useEffect(() => {
    if (!token) { setMe(null); return; }
    api.me().then(setMe).catch(() => setMe(null));
  }, [token]);

  // Everything else that sticks — the task rail, the evidence rail, the pane
  // tabs — has to stop below the header rather than slide under it. The header
  // is not a fixed height (the strip comes and goes with the token, and the row
  // wraps on narrow screens), so it is measured rather than guessed.
  useEffect(() => {
    const el = document.querySelector("header.top");
    if (!el) return;
    const set = () => document.documentElement.style
      .setProperty("--chrome", `${Math.round(el.getBoundingClientRect().height)}px`);
    set();
    // Measured once regardless; the observer only keeps it correct as the
    // header reflows. Where ResizeObserver is missing the sticky offsets fall
    // back to the measurement taken here rather than to nothing.
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, [token, metrics]);

  // The strip is authenticated and merchant-scoped, so it only exists once
  // there is a principal. It refreshes on navigation rather than on a timer:
  // the numbers change when you approve something, and that is a navigation.
  useEffect(() => {
    if (!token) { setMetrics(null); return; }
    api.metrics().then(setMetrics).catch(() => setMetrics(null));
  }, [token, location.pathname]);

  function save() {
    setToken(draft.trim());
    setTok(draft.trim());
    setDraft("");
  }

  return (
    <ToastHost>
      {/* Keyboard users should not have to tab through the header on every
          navigation to reach the thing they came for. */}
      <a className="skip" href="#main">Skip to content</a>
      <ActivityBar />

      <header className="top">
        <div className="top-inner">
          <div className="logo">
            <Mark />
            <div>
              <div className="name">MerchantOps Agent</div>
              <span className="kicker">control plane</span>
            </div>
          </div>
          <nav className="tabs" aria-label="Sections">
            <NavLink to="/" end>Investigate</NavLink>
            <NavLink to="/dashboard">Dashboard</NavLink>
            <NavLink to="/incidents">Incidents</NavLink>
            <NavLink to="/scenarios">Scenarios</NavLink>
            <NavLink to="/operations">Operations</NavLink>
          </nav>

          <div className="top-right">
            {me ? (
              <span className="who" title={`${me.user_id} · ${me.permissions.join(", ")}`}>
                <span className="who-id">{me.user_id}</span>
                <span className="muted">{me.role} · {me.merchant_id}</span>
              </span>
            ) : null}
            <button className="icon-btn" title="Command palette (⌘K)" aria-label="Command palette"
                    onClick={() => window.dispatchEvent(
                      new KeyboardEvent("keydown", { key: "k", metaKey: true }))}>⌘</button>
            <DensityToggle />
            <ThemeToggle />
            <Link className="icon-btn" to="/settings" title="Settings" aria-label="Settings">⚙</Link>
            {isDemoSession() ? (
              <span className="chip warn" title="Everyone who opens this page shares this session">
                <span className="dot" />Shared demo
              </span>
            ) : null}
            {token ? (
              <button onClick={() => { setToken(""); setTok(""); }}>Sign out</button>
            ) : null}
          </div>
        </div>

        {token ? <OpsStrip m={metrics} /> : null}
      </header>

      <CommandPalette />

      <div className="frame">
        {/* The rail is navigation, so it is only there once there is something
            to navigate to. It never carries state the server owns. */}
        {token ? <TaskRail /> : null}

        <main className="shell" id="main">
          <RunNotices health={health} />
          {/* Keyed on the path so each navigation mounts a fresh subtree and the
              entrance animation actually runs. Under prefers-reduced-motion the
              animation is neutralised in CSS; the key change is harmless. */}
          <div className="route" key={location.pathname}>
            {token
              ? <Outlet context={{ me, health, onHealth: setHealth }} />
              : <SignIn draft={draft} setDraft={setDraft} save={save} />}
          </div>
        </main>
      </div>
    </ToastHost>
  );
}

/** The operations strip: what is waiting, what moved, and how the run is going.
 *
 *  Every number comes from /metrics, scoped to this merchant server-side. There
 *  is no client-side arithmetic here on purpose — a number this page computed
 *  itself would be a number nobody can audit. */
function OpsStrip({ m }: { m: Metrics | null }) {
  if (!m) return null;

  const rupees = (minor: number) =>
    `₹${(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;

  return (
    <div className="strip">
      <span className={`strip-cell ${m.gated > 0 ? "warn" : ""}`}>
        Gated <b>{m.gated}</b>
      </span>
      <span className="strip-cell">Approved {m.window_hours}h <b>{m.approved}</b></span>
      <span className="strip-cell">Moved <b>{rupees(m.moved_minor)}</b></span>
      <span className={`strip-cell ${m.rejected > 0 ? "danger" : ""}`}>
        Rejected <b>{m.rejected}</b>
      </span>
      <span className="strip-cell">
        Tool err{" "}
        {/* Unknown is not zero. Over no calls there is no rate to report. */}
        <b>{m.tool_error_rate === null
          ? "—"
          : `${(m.tool_error_rate * 100).toFixed(1)}%`}</b>
      </span>
      <span className="strip-cell">
        P50 <b>{m.p50_duration_ms === null ? "—" : `${m.p50_duration_ms}ms`}</b>
      </span>
      {m.signing_secret_is_development_default ? (
        <span className="strip-cell danger">Signing secret <b>dev</b></span>
      ) : null}
    </div>
  );
}

/** Recently opened tasks, pinned to the left of every page.
 *
 *  Local navigation only. Tasks belong to the merchant and live server-side;
 *  this list is not the record, and the rail says so rather than letting the
 *  placement imply otherwise. */
function TaskRail() {
  const [recent, setRecent] = useState<RecentTask[]>(readRecent);
  useEffect(() => subscribeRecent(setRecent), []);

  return (
    <aside className="rail" aria-label="Recent tasks">
      <div className="rail-head">
        Tasks
        <span className="count">{recent.length}</span>
      </div>

      {/* Starting the next investigation is the most common thing to do from a
          task page, and it was a trip back through the nav to reach. The state
          flag asks Investigate to put the cursor in the box, so the action is
          click-then-type rather than click-then-click-then-type. */}
      <NavLink className="rail-new" to="/" state={{ focus: true }} end>
        <span aria-hidden="true">+</span> New investigation
      </NavLink>

      {recent.length === 0 ? (
        <p className="rail-empty">
          Nothing yet. A task you open appears here, in this browser only.
        </p>
      ) : (
        <>
          <ul className="rail-list">
            {recent.map((r) => (
              <li key={r.id}>
                <NavLink to={`/tasks/${r.id}`} data-s={r.status ?? ""}>
                  <span className="rail-id">{r.id}</span>
                  <span className="rail-q">{r.request}</span>
                  {r.status ? <span className="rail-s">{r.status.replace(/_/g, " ")}</span> : null}
                </NavLink>
              </li>
            ))}
          </ul>
          <div className="rail-foot">
            <button onClick={forgetRecent}>Clear list</button>
            <span className="muted">Local only — the audit trail is server-side.</span>
          </div>
        </>
      )}
    </aside>
  );
}

/** The run configuration, as one line.
 *
 *  This used to be three stacked banners on top of every page — orange, green
 *  and red — which is a wall, not a warning. A warning that is always shouting
 *  is one people learn to scroll past.
 *
 *  So the *facts* are a single line that is always there, and the paragraphs
 *  explaining them are one disclosure away. Nothing was deleted: every sentence
 *  is still here, and still on the first screen. The one exception is a dead
 *  API, which is not a disclosure — it means nothing else on the page is true,
 *  so it keeps its banner. */
function RunNotices({ health }: { health: Health | null }) {
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
    <details className="notice">
      <summary>
        {/* The line states the facts; the body explains them. Deliberately not
            the same sentences twice — a summary that repeats its own disclosure
            is just the banner again, only narrower. */}
        <span className="notice-line">
          {real
            ? <>Execution is <strong>live</strong> against Razorpay test mode.</>
            : <>Execution is <strong>mocked</strong>.</>}{" "}
          {model
            ? <>Reasoning <code>{health.llm_model}</code>.</>
            : <>Reasoning is the <strong>deterministic planner</strong>.</>}
          {devSecret
            ? <> Signing secret is the <strong className="is-danger">development default</strong>.</>
            : null}
        </span>
        <span className="notice-more">what this means</span>
      </summary>

      <div className="notice-body">
        {!real ? (
          <p>
            <strong>Refunds execute against a mock adapter</strong>, not Razorpay
            (<code>{health.payment_adapter}</code>). Policy, approval, idempotency and
            verification are identical on both paths — only the outbound call differs.
          </p>
        ) : (
          <p><strong>Live Razorpay Test Mode.</strong> Approved refunds hit the provider.</p>
        )}

        {!model ? (
          <p>
            <strong>Reasoning is the deterministic planner</strong>, not a language model
            {health.llm_credential_source === null
              ? " (no Anthropic credential detected)"
              : ` (LLM_PROVIDER is set explicitly)`}
            . Results measure the control plane, not model intelligence.
          </p>
        ) : (
          <p>
            Reasoning: <code>{health.llm_model}</code>, authenticated via{" "}
            <code>{health.llm_credential_source}</code>.
          </p>
        )}

        {devSecret ? (
          <p className="is-danger">
            <strong>Development signing secret in use.</strong> Tokens minted here are
            forgeable by anyone with the source. Set <code>API_TOKEN_SECRET</code> before
            this is reachable by anyone else.
          </p>
        ) : null}
      </div>
    </details>
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
