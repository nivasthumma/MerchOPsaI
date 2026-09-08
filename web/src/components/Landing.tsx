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
