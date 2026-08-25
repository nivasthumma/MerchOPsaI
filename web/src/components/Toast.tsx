// Transient confirmation. Deliberately *additive*: every outcome a toast
// mentions is also written into the page, because a message that disappears
// after four seconds must never be the only record that a refund executed.

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "danger";
interface Toast { id: number; tone: Tone; title: string; body?: string }

const Ctx = createContext<(t: Omit<Toast, "id">) => void>(() => {});

export function useToast() {
  return useContext(Ctx);
}

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);

  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { ...t, id }]);
    // Failures stay until dismissed; a refusal that vanishes on its own is how
    // someone concludes an action succeeded.
    if (t.tone === "ok") {
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 4200);
    }
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            <button className="close" aria-label="Dismiss"
                    onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>×</button>
            <strong>{t.title}</strong>
            {t.body ? <span className="muted">{t.body}</span> : null}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
