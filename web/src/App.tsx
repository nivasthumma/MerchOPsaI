import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { api, getToken, isDemoSession, setToken } from "./api/client";
import type { Health, Metrics, Principal } from "./api/types";
import { ActivityBar, DensityToggle } from "./components/Chrome";
import {
  ForkDiagram, LadderMark, LandingHeader, LimitMark, ScreenCarousel, SECTIONS,
  SectionHead, settleReveals, StateMark, useReveal,
} from "./components/Landing";
import { ThemeToggle } from "./components/Theme";
import { CommandPalette } from "./components/CommandPalette";
import { ToastHost } from "./components/Toast";
import { readRecent, subscribeRecent, forgetRecent, type RecentTask } from "./recent";

export default function App() {
  const location = useLocation();
  const nav = useNavigate();
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
    // Off the sign-in page once there is a token. Leaving somebody on
    // `/signin` after they have signed in means the address bar disagrees with
    // the screen, and the back button walks them into a page that no longer
    // exists for them.
    if (location.pathname === "/signin") nav("/", { replace: true });
  }

  // Signed out, this is a public landing page and nothing else: no operations
  // header, no navigation to screens that need a token, no "execution is
  // mocked" strip. Carrying the console's chrome onto a page whose only job is
  // to explain the product and let someone in made it read as an app that had
  // failed to load rather than as a front door.
  //
  // Same address and same port either way -- `/` is the landing until a token
  // exists and the Command Center after it.
  if (!token) {
    // Two signed-out pages, not one page with an anchor. Signing in is a
    // decision with its own address: it can be linked to, bookmarked, and
    // returned to by the back button, and the page it lives on owes the reader
    // nothing except the way in.
    const signingIn = location.pathname === "/signin";
    return (
      <ToastHost>
        <a className="skip" href="#main">Skip to content</a>
        <main className={signingIn ? "auth-page" : "landing-page"} id="main">
          {signingIn
            ? <SignIn draft={draft} setDraft={setDraft} save={save} />
            : <Landing health={health} />}
        </main>
      </ToastHost>
    );
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
          <MainNav />

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
            <Outlet context={{ me, health, onHealth: setHealth }} />
          </div>
        </main>
      </div>
    </ToastHost>
  );
}

/** The information architecture of §29, as navigation — plan P1-01.
 *
 *  It was five flat tabs in the order they were built, which is a menu rather
 *  than a structure. The four groups say what each screen is *for*: what needs
 *  doing, what the system found, whether you can believe it, and how it is
 *  configured. That grouping is the plan's, and it is worth keeping because it
 *  matches how the work actually splits — an operator lives in OPERATIONS, an
 *  auditor lives in TRUST, and neither wants the other's screens in their way.
 *
 *  Rendered as one <nav> with labelled groups rather than four navs: a screen
 *  reader should hear one navigation landmark with sections inside it, not four
 *  competing ones. */
function MainNav() {
  const groups: [string, [string, string, boolean?][]][] = [
    ["", [["/", "Command Center", true]]],
    ["Operations", [
      ["/incidents", "Incidents"],
      ["/actions", "Actions"],
      ["/recovery", "Recovery"],
    ]],
    ["Intelligence", [
      ["/investigate", "Investigate"],
      ["/dashboard", "Dashboard"],
    ]],
    ["Trust", [
      ["/operations", "Reconciliation"],
      // The live event record (ADR/v2 §65). It keeps its place in the nav:
      // the route survived the merge and a reachable-only-by-URL screen is
      // one nobody finds.
      ["/timeline", "Timeline"],
      ["/scenarios", "Evaluation"],
    ]],
  ];

  return (
    <nav className="tabs" aria-label="Sections">
      {groups.map(([label, items]) => (
        <span className="tab-group" key={label || "home"}>
          {label ? <span className="tab-group-label" aria-hidden="true">{label}</span> : null}
          {items.map(([to, text, end]) => (
            <NavLink key={to} to={to} end={end}>{text}</NavLink>
          ))}
        </span>
      ))}
    </nav>
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
      {/* Toned like every other cell here: the strip already says "gated" in
          clay and "rejected" in red, and a tool error rate of 26.7% sat in
          plain grey beside them. A number that means something is wrong should
          not be the calmest thing on the row.
          Ten per cent is the threshold the taxonomy treats as a degraded
          provider rather than noise; below it, the rate is information. */}
      <span className={`strip-cell ${
        m.tool_error_rate !== null && m.tool_error_rate >= 0.1 ? "danger"
        : m.tool_error_rate !== null && m.tool_error_rate > 0 ? "warn" : ""}`}>
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
      <NavLink className="rail-new" to="/investigate" state={{ focus: true }} end>
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

/** The public page. Explains the product and points at the way in. */
function Landing({ health }: { health: Health | null }) {
  const rShots = useReveal<HTMLElement>();
  const rProblem = useReveal<HTMLElement>();
  const rLadder = useReveal<HTMLElement>();
  const rStates = useReveal<HTMLElement>();
  const rMeasured = useReveal<HTMLElement>();
  const rLimits = useReveal<HTMLElement>();

  // The public face of the product AND the way in, on one port. Signed out,
  // `/` is this page; signing in replaces it with the Command Center. The
  // token panel stays in the document rather than behind a route, so the "Sign
  // in" control is a scroll and not a navigation -- one page, one address.
  return (
    <div className="landing" id="top">
      {/* The section list is back at the top, where a reader who is halfway
          down a long page can see it. It is not the list of links it was: the
          pill follows the section actually on screen, so the header answers
          "where am I" rather than only "where can I go". */}
      <LandingHeader tools={<>
        {/* The page follows the viewer's system theme and this overrides it.
            It belongs here rather than only inside the console: somebody
            deciding whether they trust a financial tool should not have to
            sign in first to read it on the ground they prefer. */}
        <ThemeToggle />
        <Link className="lp-btn sm" to="/signin">Sign in</Link>
      </>} />

      {/* Under the header and on the page's own measure, not floated above it.
          This is the disclosure that execution is mocked, that the planner is
          deterministic and that the signing secret is the development default,
          and the front door is exactly where an unfamiliar reader most needs
          it -- but it is part of the page, not a bar bolted to the top of it. */}
      <RunNotices health={health} />

      <section className="lp-hero is-entering">
        <div className="lp-hero-copy">
          <p className="lp-eyebrow">Payments control plane</p>
          <h2 className="lp-h1">
            An HTTP 200 is not <span>a business outcome.</span>
          </h2>
          <p className="lp-sub">
            An agent that investigates payment incidents and can refund money.
            The hard part was never the reasoning — it was making sure that
            when the system says a refund happened, <b>a refund happened</b>.
          </p>
          <div className="lp-cta">
            <Link className="lp-btn" to="/signin">Sign in <span aria-hidden="true">→</span></Link>
            <a className="lp-btn ghost" href="#states">What it does</a>
          </div>
          <ul className="lp-ticks">
            <li><b>✓</b> Policy decided outside the model</li>
            <li><b>✓</b> Every action read back</li>
            <li><b>✓</b> Uncertainty is a state</li>
          </ul>
        </div>

        {/* One refund climbing the ladder and stopping at the honest answer.
            The thesis, rather than a screenshot of it. */}
        <div className="lp-panel">
          <div className="lp-panel-top">
            Refund <b>ACT_D6412DD8B500</b>
            <span className="lp-tag"><i aria-hidden="true" /> unresolved</span>
          </div>
          <ol className="lp-steps">
            <li>
              <span className="g ok" aria-hidden="true">✓</span>
              <span className="t">Duplicate found and evidence gathered
                <small>2 independent reads</small></span>
              <span className="v">Recorded</span>
            </li>
            <li>
              <span className="g hold" aria-hidden="true">◼</span>
              <span className="t">Policy required a person
                <small>high_risk_requires_approval</small></span>
              <span className="v">Held</span>
            </li>
            <li>
              <span className="g ok" aria-hidden="true">✓</span>
              <span className="t">Approved, then submitted to the provider
                <small>idempotency key derived server-side</small></span>
              <span className="v">Sent</span>
            </li>
            <li>
              <span className="g unk" aria-hidden="true">?</span>
              <span className="t">The response never arrived
                <small>attempt 1 of 5 · next check in 30s</small></span>
              <span className="v is-unk">Unknown</span>
            </li>
          </ol>
          <p className="lp-panel-foot">
            The refund may have landed. It may not have. The system records
            <b> UNKNOWN</b> and re-reads provider state on a schedule — it never
            retries, because retrying an unknown financial action is how you
            refund twice.
          </p>
        </div>
      </section>

      <section className="lp-shots" id="console" ref={rShots}>
        <SectionHead kicker="The console" title="Four screens, as they actually render">
          Real screenshots of the running application against seeded data, not
          mockups. Everything below is what an operator sees after signing in.
        </SectionHead>
        <ScreenCarousel />
      </section>

      <section id="problem" ref={rProblem}>
        <SectionHead kicker="The problem"
                     title="The provider accepting is not the money moving">
          A refund call returns <code>200 OK</code> with a refund id. Most
          systems record that as done. It is not done — it is <b>submitted</b>.
        </SectionHead>
        {/* The diagram is the argument, not an illustration of it: one
            response, three things that can have happened to the money, and
            nothing in the response saying which. */}
        <div className="lp-split">
          <ForkDiagram />
          <div className="lp-split-copy">
            <p className="lp-lede">
              A provider can accept a request and apply less than was asked, or
              nothing at all, and the response looks identical either way.
            </p>
            <p className="lp-lede">
              Worse is when the response never arrives. A system that guesses
              there either refunds twice, or tells a merchant their customer
              was paid when they were not.
            </p>
          </div>
        </div>
      </section>

      <section id="ladder" ref={rLadder}>
        <SectionHead kicker="How it works"
                     title="Four steps, and only the last one is evidence">
          Nothing is recorded as done until the provider has been read back
          independently. The first three are claims.
        </SectionHead>
        <div className="lp-cards is-stagger">
          <div className="lp-card lp-card-mark">
            <LadderMark step={1} />
            <div><h4>01 · PROPOSED</h4>
            <p>The agent reasons broadly and can propose anything. Proposing is
              free; nothing has happened.</p></div></div>
          <div className="lp-card lp-card-mark">
            <LadderMark step={2} />
            <div><h4>02 · GATED</h4>
            <p>Deterministic policy, outside the model. A human signs for
              anything that moves money.</p></div></div>
          <div className="lp-card lp-card-mark">
            <LadderMark step={3} />
            <div><h4>03 · SUBMITTED</h4>
            <p>A <code>200</code> and a refund id. An idempotency key derived
              server-side means a retry cannot double-refund.</p></div></div>
          <div className="lp-card lp-card-mark">
            <LadderMark step={4} />
            <div><h4>04 · VERIFIED</h4>
            <p>A separate read, against provider state, after the fact. Only
              this step is evidence.</p></div></div>
        </div>
      </section>

      <section className="lp-states" id="states" ref={rStates}>
        <SectionHead kicker="The four states"
                     title="Uncertainty is a state, not an error">
          Reading the provider back gives one of four answers, and the fourth is
          the one the rest of the system is built around.
        </SectionHead>
        <div className="lp-cards is-stagger">
          <div className="lp-card c-ok"><StateMark kind="ok" /><h4>SUCCESS</h4>
            <p>Verified at the provider. The money moved.</p></div>
          <div className="lp-card c-failed"><StateMark kind="failed" /><h4>FAILED</h4>
            <p>Verified as not having taken effect. No money moved.</p></div>
          <div className="lp-card c-partial"><StateMark kind="partial" /><h4>PARTIAL</h4>
            <p>Accepted, but the provider reflects less than was asked.</p></div>
          <div className="lp-card c-unknown"><StateMark kind="unknown" /><h4>UNKNOWN</h4>
            <p>Could not be established. Unresolved work on a backoff schedule,
              escalating to a person after five attempts. Never retried.</p></div>
        </div>
      </section>

      <section id="measured" ref={rMeasured}>
        <SectionHead kicker="Measured"
                     title="Numbers the repository can reproduce">
          Every figure comes from a command, and a check in CI fails the build
          when the documentation and the tree disagree about any of them.
        </SectionHead>
        <div className="lp-stats is-stagger">
          <div><b>187<i>/187</i></b><span>Scenarios</span>
            <em>110 of them critical</em></div>
          <div><b>136<i>/136</i></b><span>Injected defects caught</span>
            <em>every control has a test that fails when it breaks</em></div>
          <div><b>1410</b><span>Automated tests</span>
            <em>1086 backend · 324 frontend</em></div>
          <div><b>0</b><span>Dependency advisories</span>
            <em>both ecosystems, pinned</em></div>
        </div>

        {/* The command itself, and what it prints. A section claiming its
            numbers are reproducible and then only restating them is asking to
            be taken at its word; this is the check that would fail the build,
            and every figure in it is one the check gates. */}
        <figure className="lp-term">
          <figcaption>
            <span className="lp-term-dots" aria-hidden="true"><i /><i /><i /></span>
            make counts
          </figcaption>
          {/* A line per element, not one text node with newlines in it: JSX
              trims the whitespace at the ends of its lines, so a transcript
              written that way renders as a single run-on line. */}
          <pre><code>
            <span className="ln"><b>$</b> make counts</span>
            <span className="ln">
              {"measured:  1086 python tests · 324 vitest · 187 scenarios · 136 mutants"}
            </span>
            <span className="ln">{"browser:   14 Playwright tests defined"}</span>
            <span className="ln ok">✓ published numbers match what the tree measures</span>
          </code></pre>
        </figure>
      </section>

      <section id="limits" ref={rLimits}>
        <SectionHead kicker="Not claimed" title="What this does not do">
          A page that lists only strengths is marketing. These are the limits,
          in the repository&apos;s own words.
        </SectionHead>
        <ul className="lp-limits">
          {/* Deliberately does not restate the banner above, which already
              says execution is mocked and that the controls around it are
              unchanged. Saying it twice on one page is how a disclosure starts
              reading as boilerplate. */}
          <li><LimitMark /><b>No refund has ever reached Razorpay.</b> Not once, in any
            environment — so every claim on this page about verification is a
            claim about a mock provider answering honestly.</li>
          <li><LimitMark /><b>Reconciliation is a sweep, not a daemon.</b> An UNKNOWN action
            is re-read on a schedule somebody has to run.</li>
          <li><LimitMark /><b>21 of 590 payments are externally mapped</b>, and not one
            mapping has been confirmed against a real provider. Null means
            nobody checked.</li>
          <li><LimitMark /><b>The reasoning model has never run in anger.</b> The default
            planner is deterministic, which is what makes the evaluation
            reproducible and also a weaker test of the agent.</li>
        </ul>
      </section>

      <footer className="lp-foot">
        {/* The same list the header carries, from the same source — two
            hand-written copies of a page's own contents drift, and the one at
            the foot is the copy nobody notices has. */}
        <nav className="lp-foot-nav" aria-label="Back to a section">
          {SECTIONS.map((sec) => (
            <a key={sec.id} href={`#${sec.id}`} onClick={settleReveals}>{sec.label}</a>
          ))}
        </nav>
        <p className="lp-foot-note">
          Synthetic data throughout. Not affiliated with Razorpay.
        </p>
      </footer>

    </div>
  );
}

/** The way in, on its own address.
 *
 *  Split down the middle: the left half states what this is and stays dark in
 *  either theme, the right half is the form on a plain ground. A credential
 *  screen should look like one thing to do, and the split is what stops the
 *  explanation and the field competing for the same attention. */
function SignIn(
  { draft, setDraft, save }:
  { draft: string; setDraft: (s: string) => void; save: () => void },
) {
  return (
    <div className="auth">
      <aside className="auth-aside">
        <Link className="auth-brand" to="/">
          <span aria-hidden="true">◨</span> MerchantOps
        </Link>
        <h2 className="auth-quote">
          An HTTP 200 is not <span>a business outcome.</span>
        </h2>
        <p className="auth-quote-sub">
          Every financial action here is read back at the provider before it
          counts as done.
        </p>
        <ul className="auth-states" aria-label="Verification states">
          <li><b className="ok">✓</b> SUCCESS<em>the money moved</em></li>
          <li><b className="danger">✕</b> FAILED<em>no money moved</em></li>
          <li><b className="warn">◐</b> PARTIAL<em>less than was asked</em></li>
          <li><b className="unknown">?</b> UNKNOWN<em>not established</em></li>
        </ul>
        <Link className="auth-back" to="/">← Back to the overview</Link>
      </aside>

      <div className="auth-form">
        <div className="signin-panel">
          <h2 className="signin-h">Sign in</h2>
          <p className="signin-sub">
            A token identifies you. Everything you can do is read from the
            database on every request.
          </p>

          <div className="signin-field">
            <label htmlFor="tok">
              Mint one with <code>make token USER_ID=USR_A_OWNER</code>
            </label>
            <input
              id="tok" type="password" value={draft} placeholder="paste token"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") save(); }}
            />
            <button className="primary" onClick={save} disabled={!draft.trim()}>
              Use token
            </button>
          </div>

          <p className="signin-note">
            The token carries no permissions — those are read from the database
            on every request, so a token cannot grant itself authority. It is
            stored in this browser only and sent nowhere but the API.
          </p>
        </div>
      </div>
    </div>
  );
}
