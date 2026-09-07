import { useEffect, useRef, useState } from "react";

export interface LiveRefreshState {
  live: boolean;
  lastUpdated: string | null;
}

/**
 * Bounded foreground refresh for operational views.
 *
 * This deliberately does not pretend to be a websocket: the backend already
 * exposes a cursor-based event stream, while authenticated browser clients
 * cannot safely use EventSource without moving credentials into a URL. For
 * dashboard/list/detail views, bounded polling is the safer transport. The
 * dedicated Timeline screen remains the event-by-event stream surface.
 */
export function useLiveRefresh(
  refresh: () => Promise<unknown> | unknown,
  intervalMs = 5000,
): LiveRefreshState {
  const refreshRef = useRef(refresh);
  const [live, setLive] = useState(!document.hidden);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    let running = false;

    const run = async () => {
      if (running || document.hidden) return;
      running = true;
      try {
        await refreshRef.current();
        setLastUpdated(new Date().toISOString());
      } finally {
        running = false;
      }
    };

    const start = () => {
      if (timer || document.hidden) return;
      void run();
      timer = setInterval(() => void run(), intervalMs);
      setLive(true);
    };

    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
      setLive(false);
    };

    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs]);

  return { live, lastUpdated };
}
