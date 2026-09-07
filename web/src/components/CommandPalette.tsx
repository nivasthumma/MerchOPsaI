// ⌘K / Ctrl-K. Navigation and the few global actions, keyboard-first.
//
// Deliberately excluded: anything that moves money. Approving a refund from a
// fuzzy-matched list, two keystrokes after typing three letters, is exactly the
// kind of frictionless action this system exists to prevent. Approval happens
// on the task page, next to the evidence, or it does not happen.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useModalFocus } from "../hooks/useModalFocus";
import { api } from "../api/client";
import type { SearchHit } from "../api/types";

export interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

export function CommandPalette({ extra = [] }: { extra?: Command[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const nav = useNavigate();

  const commands = useMemo<Command[]>(() => [
    // Plan P1-07's list. Every one is a navigation or a display preference;
    // see the note at the top of this file for why none of them move money.
    { id: "go-home", label: "Open Command Center", hint: "what needs attention",
      run: () => nav("/") },
    { id: "go-actions", label: "Open approvals", hint: "the human gate",
      run: () => nav("/actions?section=awaiting_approval") },
    { id: "go-unknown", label: "Open UNKNOWN", hint: "unresolved financial work",
      run: () => nav("/actions?section=unknown") },
    { id: "go-recovery", label: "Open Recovery", hint: "the revenue ledger",
      run: () => nav("/recovery") },
    { id: "go-incidents", label: "Open Incidents", hint: "detected problems",
      run: () => nav("/incidents") },
    { id: "go-investigate", label: "Start an investigation", hint: "ask the agent",
      run: () => nav("/investigate") },
    { id: "go-scenarios", label: "Open Evaluation", hint: "scenario suite",
      run: () => nav("/scenarios") },
    { id: "go-operations", label: "Open Reconciliation", hint: "the sweep and its queue",
      run: () => nav("/operations") },
    { id: "theme", label: "Cycle theme", hint: "system, light, dark", run: () => {
      const root = document.documentElement;
      const now = root.getAttribute("data-theme");
      const next = now === null ? "light" : now === "light" ? "dark" : null;
      if (next) { root.setAttribute("data-theme", next); localStorage.setItem("merchantops.theme", next); }
      else { root.removeAttribute("data-theme"); localStorage.removeItem("merchantops.theme"); }
    } },
    { id: "density", label: "Toggle density", hint: "comfortable or compact", run: () => {
      const root = document.documentElement;
      const compact = root.getAttribute("data-density") === "compact";
      root.setAttribute("data-density", compact ? "comfortable" : "compact");
      try {
        if (compact) localStorage.removeItem("merchantops.density");
        else localStorage.setItem("merchantops.density", "compact");
      } catch { /* preference will not survive a reload */ }
    } },
    ...extra,
  ], [nav, extra]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || (c.hint ?? "").toLowerCase().includes(q));
  }, [commands, query]);

  // Global search — plan P1-06. The same box: an operator arriving with an
  // identifier in the clipboard should not have to know which of six screens
  // owns it.
  //
  // Only for input that could be an identifier. Every id in this system is at
  // least a few characters and contains no spaces, and firing a request on "g"
  // would put a query on the database for every keystroke of every command.
  const searchable = query.trim();
  const couldBeId = searchable.length >= 4 && !/\s/.test(searchable);

  useEffect(() => {
    if (!open || !couldBeId) { setHits([]); return; }
    let cancelled = false;
    // Debounced: identifiers are pasted, so waiting a beat costs nothing and
    // saves a request per character on the rare occasion one is typed.
    const t = setTimeout(() => {
      api.search(searchable)
        .then((r) => { if (!cancelled) setHits(r.results); })
        // A failed search is silently no results rather than an error state in
        // a palette. The screens themselves report connectivity; a dropdown
        // is the wrong place to raise it.
        .catch(() => { if (!cancelled) setHits([]); });
    }, 180);
    return () => { cancelled = true; clearTimeout(t); };
  }, [open, searchable, couldBeId]);

  const close = useCallback(() => {
    setOpen(false); setQuery(""); setCursor(0); setHits([]);
  }, []);

  // Search results are commands too, so one cursor moves through both and
  // Enter does the same thing wherever it is. Two lists with two cursors is
  // two keyboard models in one dialog.
  const all = useMemo<Command[]>(() => [
    ...hits.map((h) => ({
      id: `hit-${h.kind}-${h.id}`,
      label: `${h.label ?? h.id}`,
      hint: `${h.kind}${h.detail ? ` · ${h.detail}` : ""}`,
      run: () => nav(h.route),
    })),
    ...matches,
  ], [hits, matches, nav]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
      // Escape is `useModalFocus`'s, not this listener's. Handling it in both
      // would be two closes on one press — harmless today and exactly the kind
      // of duplication that stops being harmless when one of them grows a side
      // effect.
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  // P1-12. This declares `aria-modal="true"`, which tells assistive technology
  // the page behind it is inert. It focused the input and left it there: Tab
  // walked straight out into that supposedly-inert page, and closing dropped
  // focus on <body> so the next Tab restarted from the top. Same hook as the
  // action drawer, so the two cannot keep different halves of one promise.
  //
  // The input takes focus here rather than the panel, because typing is the
  // entire reason anyone opens this.
  useModalFocus(panelRef, { onClose: close, initial: inputRef, active: open });

  if (!open) return null;

  return (
    <div className="palette-scrim" onClick={close}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette"
           ref={panelRef} tabIndex={-1}
           onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef} type="text" value={query} placeholder="Command, or paste an ID…"
          aria-label="Command" autoComplete="off"
          onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, all.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
            else if (e.key === "Enter" && all[cursor]) { all[cursor].run(); close(); }
          }} />
        <ul role="listbox" aria-label="Commands">
          {all.length === 0 ? (
            <li className="muted">
              {couldBeId
                ? `No command matches, and no payment, order, customer, incident, task, action or provider reference has the id "${searchable}".`
                : "Nothing matches."}
            </li>
          ) : null}
          {all.map((c, i) => (
            <li key={c.id} role="option" aria-selected={i === cursor}
                className={i === cursor ? "on" : ""}
                onMouseEnter={() => setCursor(i)}
                onClick={() => { c.run(); close(); }}>
              <span>{c.label}</span>
              {c.hint ? <span className="muted">{c.hint}</span> : null}
            </li>
          ))}
        </ul>
        <div className="palette-foot muted">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> run</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
