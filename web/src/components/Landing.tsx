/** The landing page's moving parts — a screen carousel and scroll reveals.
 *
 *  ## Why this is CSS and an observer rather than a motion library
 *
 *  The obvious answer to "add motion" is `framer-motion`, and it would be a
 *  fine answer on a marketing site. This is a payments console: its
 *  dependencies are pinned, hashed and audited on every build, and the whole
 *  frontend ships in 369 kB. Adding a runtime animation library so that a
 *  front page can fade sections in is a permanent cost on the bundle every
 *  operator downloads, paid for a page they see once.
 *
 *  Everything here is transform and opacity — the two properties a browser can
 *  animate on the compositor — driven by one IntersectionObserver. If the
 *  motion ever needs orchestration this cannot express, the library becomes
 *  worth its weight and this is the note that says so.
 */
import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

/** Reveal children as they arrive, once.
 *
 *  `once` matters: a section that re-animates every time it scrolls back into
 *  view turns a page into a slideshow the reader cannot get past. And the
 *  resting state is VISIBLE — the element starts painted and is only hidden if
 *  the observer is actually running — so a page with JavaScript disabled, or a
 *  screenshot taken before it runs, shows the content rather than a blank.
 */
export function useReveal<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    el.dataset.reveal = "pending";
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            (e.target as HTMLElement).dataset.reveal = "in";
            io.unobserve(e.target);
          }
        }
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.08 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return ref;
}

const SHOTS = [
  {
    src: "/shots/command-center.png",
    name: "Command Center",
    note: "What needs a person, and what the money is doing. Zero recedes so "
        + "the number that matters is the one you see.",
  },
  {
    src: "/shots/actions.png",
    name: "Action Center",
    note: "Every financial action this system has taken or is waiting to take, "
        + "in five queues. Approving happens next to the evidence, never here.",
  },
  {
    src: "/shots/incident.png",
    name: "Incidents",
    note: "Detected by deterministic rules, with the numbers that tripped them. "
        + "Severity is scannable down the column.",
  },
  {
    src: "/shots/recovery.png",
    name: "Recovery ledger",
    note: "At risk, recoverable, attempted, recovered — figures that nest, and "
        + "a check that says so when they do not.",
  },
];

/** The console, as it actually renders.
 *
 *  Real screenshots of the running application against seeded data, not
 *  mockups. A landing page for an operations tool that shows an illustration
 *  of an operations tool is telling the reader it has nothing to show them.
 */
export function ScreenCarousel() {
  const [i, setI] = useState(0);
  const [paused, setPaused] = useState(false);
  const reveal = useReveal<HTMLDivElement>();

  useEffect(() => {
    if (paused) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const t = setTimeout(() => setI((n) => (n + 1) % SHOTS.length), 5200);
    return () => clearTimeout(t);
  }, [i, paused]);

  const go = (n: number) => setI((n + SHOTS.length) % SHOTS.length);

  return (
    <div
      className="carousel" ref={reveal}
      // Advancing on its own is a convenience, not a decision. It stops the
      // moment a pointer or the keyboard is on it, because a panel that moves
      // while somebody is reading it is worse than one that never moved.
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={() => setPaused(false)}
    >
      <div className="carousel-frame">
        {SHOTS.map((s, n) => (
          <figure key={s.src} className="slide" data-on={n === i}
                  aria-hidden={n === i ? undefined : true}>
            <img src={s.src} alt={`The ${s.name} screen of the MerchantOps console`}
                 width={1360} height={850}
                 loading={n === 0 ? "eager" : "lazy"} decoding="async" />
          </figure>
        ))}
      </div>

      <div className="carousel-bar">
        <div className="carousel-said" aria-live="polite">
          <b>{SHOTS[i].name}</b>
          <span>{SHOTS[i].note}</span>
        </div>
        <div className="carousel-dots" role="tablist" aria-label="Console screens">
          {SHOTS.map((s, n) => (
            <button
              key={s.src} type="button" role="tab" aria-selected={n === i}
              aria-label={s.name} className={n === i ? "on" : ""}
              onClick={() => go(n)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- diagrams
 *
 * Drawn rather than described, because each of these says something a
 * paragraph has to spend a sentence on: that the ladder is a sequence with one
 * step that counts, and that the four states are two verified answers, one
 * partial, and one that is not an answer at all.
 *
 * Inline SVG, no library. Every shape carries an explicit fill, colours come
 * from the same tokens as the rest of the page so they hold in both themes,
 * and the viewBox leaves room for the strokes so nothing clips at the edges.
 * Each is `aria-hidden`: the heading and the copy beside it already say this
 * in words, and a screen reader should not hear it twice.
 */

/** The ladder: three claims and one piece of evidence. */
export function LadderMark({ step }: { step: 1 | 2 | 3 | 4 }) {
  const done = step === 4;
  return (
    <svg viewBox="0 0 44 44" width="40" height="40" aria-hidden="true"
         className={`lp-mark ${done ? "is-evidence" : ""}`}>
      <rect x="1.5" y="1.5" width="41" height="41" rx="11"
            fill="var(--surface-2)" stroke="var(--border)" />
      {/* Rungs fill up to the current step, so the four marks read as a
          sequence when they sit side by side. */}
      {[0, 1, 2, 3].map((n) => (
        <rect key={n} x={11} y={30 - n * 6} width={22} height={3} rx={1.5}
              fill={n < step ? (done ? "var(--ok)" : "var(--text-dim)")
                             : "var(--border-strong)"} />
      ))}
      {done ? (
        <path d="M15 15.5l4.2 4.2 8-8.4" fill="none" stroke="var(--ok)"
              strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      ) : null}
    </svg>
  );
}

/** The four states, each drawn as what it means rather than as a letter. */
export function StateMark({ kind }: { kind: "ok" | "failed" | "partial" | "unknown" }) {
  const stroke = {
    ok: "var(--ok)", failed: "var(--danger)",
    partial: "var(--warn)", unknown: "var(--unknown)",
  }[kind];
  return (
    <svg viewBox="0 0 40 40" width="34" height="34" aria-hidden="true" className="lp-state-mark">
      <circle cx="20" cy="20" r="17" fill="none" stroke={stroke}
              strokeWidth="1.6" opacity="0.45"
              /* UNKNOWN is the only one whose ring is broken: the outcome was
                 never closed, and the shape says so before the label does. */
              strokeDasharray={kind === "unknown" ? "4 5" : undefined} />
      {kind === "ok" ? (
        <path d="M13 20.5l4.6 4.6 9.4-9.8" fill="none" stroke={stroke}
              strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
      ) : null}
      {kind === "failed" ? (
        <path d="M14 14l12 12M26 14L14 26" fill="none" stroke={stroke}
              strokeWidth="2.6" strokeLinecap="round" />
      ) : null}
      {kind === "partial" ? (
        /* Half filled, to the extent the provider actually reflected. */
        <path d="M20 5.5a14.5 14.5 0 0 0 0 29z" fill={stroke} opacity="0.9" />
      ) : null}
      {kind === "unknown" ? (
        <text x="20" y="26" textAnchor="middle" fill={stroke}
              fontSize="17" fontWeight="700" fontFamily="var(--display)">?</text>
      ) : null}
    </svg>
  );
}

/* ------------------------------------------------------------------ header
 *
 * The section list used to sit at the foot, on the reasoning that a table of
 * contents competes with the one thing a visitor came to do. That was half
 * right: a list of links does. An indicator that follows the reader down the
 * page is not a list of links -- it is the page saying where you are, which is
 * the one thing a long single-scroll page cannot otherwise tell you.
 */

export const SECTIONS = [
  { id: "console", label: "The console" },
  { id: "problem", label: "The problem" },
  { id: "ladder", label: "How it works" },
  { id: "states", label: "The four states" },
  { id: "measured", label: "Measured" },
  { id: "limits", label: "Not claimed" },
] as const;

/** Every section opens the same way: what it is, what it says, and why.
 *
 *  The kicker is not decoration -- it is the label that section carries in the
 *  header, so a reader who followed "How it works" lands on a block that says
 *  "How it works" back to them. Without it each section opened on a sentence
 *  in display type with nothing naming it, and a page of six of those reads as
 *  six unrelated statements rather than one argument.
 */
export function SectionHead(
  { kicker, title, children }:
  { kicker: string; title: ReactNode; children?: ReactNode },
) {
  return (
    <header className="lp-sec-head">
      <p className="lp-kicker">{kicker}</p>
      <h3 className="lp-h2">{title}</h3>
      {children ? <p className="lp-lede">{children}</p> : null}
    </header>
  );
}

/** Which section the reader is actually looking at.
 *
 *  The band is the middle of the viewport rather than the top: a heading that
 *  has only just crossed the top edge is not what somebody is reading, and a
 *  spy anchored there lights the next section up while the previous one still
 *  fills the screen.
 */
export function useActiveSection(ids: readonly string[]) {
  const [active, setActive] = useState<string | null>(null);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const seen = new Map<string, boolean>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) seen.set(e.target.id, e.isIntersecting);
        // First in document order wins, so scrolling up lands on the section
        // whose top is nearest rather than on whichever fired last.
        setActive(ids.find((id) => seen.get(id)) ?? null);
      },
      { rootMargin: "-45% 0px -50% 0px" },
    );
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
  }, [ids]);
  return active;
}

/** True once the page has moved at all. */
function useScrolled() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 8);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);
  return scrolled;
}

/** The landing page's header: brand, the section spy, and the way in.
 *
 *  Full width rather than on the page's measure, because a bar that stops
 *  short of the window edge reads as a floating panel that happens to be at
 *  the top, not as the page's header.
 */
export function LandingHeader({ tools }: { tools: ReactNode }) {
  const active = useActiveSection(SECTIONS.map((s) => s.id));
  const scrolled = useScrolled();
  const navRef = useRef<HTMLElement | null>(null);
  const [ind, setInd] = useState<{ x: number; w: number } | null>(null);

  // The pill slides between links instead of each link painting its own. One
  // moving object is legible; five fading in and out is a flicker. Measured
  // from layout, so it survives the labels changing length or wrapping.
  useEffect(() => {
    const nav = navRef.current;
    const on = nav?.querySelector<HTMLElement>("a.on");
    if (!nav || !on) return setInd(null);
    const a = on.getBoundingClientRect();
    const b = nav.getBoundingClientRect();
    if (a.width === 0) return setInd(null);
    setInd({ x: a.left - b.left, w: a.width });
  }, [active]);

  return (
    <header className={`lp-topwrap${scrolled ? " is-stuck" : ""}`}>
      <div className="lp-top">
        <a className="lp-brand" href="#top">
          <span aria-hidden="true">◨</span> MerchantOps
        </a>

        <nav
          className="lp-nav" aria-label="On this page" ref={navRef}
          style={ind
            ? ({ "--ind-x": `${ind.x}px`, "--ind-w": `${ind.w}px` } as CSSProperties)
            : undefined}
          data-ind={ind ? "on" : undefined}
        >
          {SECTIONS.map((s) => (
            <a
              key={s.id} href={`#${s.id}`} className={active === s.id ? "on" : ""}
              aria-current={active === s.id ? "true" : undefined}
            >
              {s.label}
            </a>
          ))}
        </nav>

        <div className="lp-tools">{tools}</div>
      </div>
      {/* How far down the page the reader is. Driven by the scroller itself
          where the browser supports it, and simply absent where it does not --
          a progress bar is worth no JavaScript on a scroll handler. */}
      <span className="lp-progress" aria-hidden="true" />
    </header>
  );
}

/** One response, three truths.
 *
 *  The section beside this says a `200 OK` is not a business outcome. The
 *  drawing is the argument: one response fans out to three different things
 *  that can have happened to the money, and nothing in the response tells you
 *  which. Saying that takes a paragraph; showing it takes a fork.
 *
 *  The connectors draw themselves on reveal, left to right, because the order
 *  is the point -- the request, then the acknowledgement, then the divergence.
 */
export function ForkDiagram() {
  const T = (x: number, y: number, s: string, cls = "") => (
    <text x={x} y={y} className={`fd-t ${cls}`}>{s}</text>
  );
  const outcomes: Array<[number, string, string, string]> = [
    [34, "SUCCESS", "var(--ok)", "the money moved"],
    [116, "PARTIAL", "var(--warn)", "less than was asked"],
    [198, "UNKNOWN", "var(--unknown)", "no answer ever came"],
  ];
  return (
    <svg viewBox="0 0 582 262" className="fd" role="img"
         aria-label={"One 200 OK response, and the three different things that "
                   + "can have happened to the money behind it."}>
      {/* the call */}
      <rect x="2" y="106" width="126" height="46" rx="10"
            fill="var(--surface-2)" stroke="var(--border-strong)" />
      {T(20, 127, "POST")}
      {T(20, 142, "/refunds", "fd-dim")}

      {/* the acknowledgement -- the only thing the caller is ever handed */}
      <rect x="176" y="106" width="140" height="46" rx="10"
            fill="var(--surface)" stroke="var(--accent-border)" strokeWidth="1.4" />
      {T(194, 127, "200 OK", "fd-acc")}
      {T(194, 142, "rfnd_9c2f", "fd-dim")}

      <path d="M128 129h44" fill="none" stroke="var(--border-strong)" strokeWidth="1.6"
            className="fd-w" style={{ "--d": "0ms" } as CSSProperties} />
      <path d="M168 125l6 4-6 4z" fill="var(--border-strong)" />

      {outcomes.map(([y, name, colour, note], n) => (
        <g key={name}>
          {/* A curve rather than an elbow: three elbows out of one node read as
              a decision tree, and nothing here decided anything. */}
          <path d={`M316 129C348 129 352 ${y + 22} 386 ${y + 22}`}
                fill="none" stroke={colour} strokeWidth="1.6" opacity="0.75"
                className="fd-w" style={{ "--d": `${180 + n * 130}ms` } as CSSProperties} />
          <rect x="386" y={y} width="192" height="44" rx="10"
                fill="var(--surface)" stroke={colour} strokeOpacity="0.5" />
          <rect x="386" y={y} width="3" height="44" rx="1.5" fill={colour} />
          <text x="404" y={y + 20} className="fd-t" fill={colour}>{name}</text>
          {T(404, y + 35, note, "fd-dim")}
        </g>
      ))}
    </svg>
  );
}

/** The mark beside each limit: a ring that was never closed. */
export function LimitMark() {
  return (
    <svg viewBox="0 0 18 18" width="15" height="15" aria-hidden="true" className="lp-limit-mark">
      <circle cx="9" cy="9" r="7" fill="none" stroke="var(--warn)" strokeWidth="1.6"
              strokeDasharray="3 3.4" />
      <path d="M9 5.4v4.4" stroke="var(--warn)" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="9" cy="12.4" r="0.95" fill="var(--warn)" />
    </svg>
  );
}
