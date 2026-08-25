// ⌘K / Ctrl-K. Navigation and the few global actions, keyboard-first.
//
// Deliberately excluded: anything that moves money. Approving a refund from a
// fuzzy-matched list, two keystrokes after typing three letters, is exactly the
// kind of frictionless action this system exists to prevent. Approval happens
// on the task page, next to the evidence, or it does not happen.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

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
  const inputRef = useRef<HTMLInputElement>(null);
  const nav = useNavigate();

  const commands = useMemo<Command[]>(() => [
    { id: "go-investigate", label: "Go to Investigate", hint: "ask the agent",
      run: () => nav("/") },
    { id: "go-scenarios", label: "Go to Scenarios", hint: "evaluation suite",
      run: () => nav("/scenarios") },
    { id: "go-operations", label: "Go to Operations", hint: "reconciliation and queue",
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

  const close = useCallback(() => { setOpen(false); setQuery(""); setCursor(0); }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") {
        close();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  if (!open) return null;

  return (
    <div className="palette-scrim" onClick={close}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette"
           onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef} type="text" value={query} placeholder="Type a command…"
          aria-label="Command" autoComplete="off"
          onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, matches.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
            else if (e.key === "Enter" && matches[cursor]) { matches[cursor].run(); close(); }
          }} />
        <ul role="listbox" aria-label="Commands">
          {matches.length === 0 ? <li className="muted">Nothing matches.</li> : null}
          {matches.map((c, i) => (
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
