// Two pieces of app chrome: the activity indicator and the density control.

import { useEffect, useState } from "react";
import { activity } from "../api/client";

/** A progress bar bound to real in-flight requests.
 *
 * It appears only after a short delay, because a bar that flashes on every
 * 40ms call is noise, and it reflects the actual request count rather than an
 * animation pretending to be one. */
export function ActivityBar() {
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    return activity.subscribe((n) => {
      if (n > 0 && !timer) {
        timer = setTimeout(() => setBusy(true), 180);
      } else if (n === 0) {
        if (timer) { clearTimeout(timer); timer = null; }
        setBusy(false);
      }
    });
  }, []);

  if (!busy) return null;
  return <div className="activity-bar" role="progressbar" aria-label="Loading" />;
}

type Density = "comfortable" | "compact";
const KEY = "merchantops.density";

function readDensity(): Density {
  try {
    return localStorage.getItem(KEY) === "compact" ? "compact" : "comfortable";
  } catch {
    return "comfortable";
  }
}

/** Compact mode is for the operator with forty rows to read, not for saving
 *  pixels. It tightens spacing and type; it never hides a state, a label, or a
 *  warning — density is a reading preference, not an editorial one. */
export function DensityToggle() {
  const [density, setDensity] = useState<Density>(readDensity);

  useEffect(() => {
    document.documentElement.setAttribute("data-density", density);
    try {
      if (density === "compact") localStorage.setItem(KEY, "compact");
      else localStorage.removeItem(KEY);
    } catch {
      /* the preference simply will not survive a reload */
    }
  }, [density]);

  const next = density === "comfortable" ? "compact" : "comfortable";
  return (
    <button className="icon-btn" title={`Density: ${density} — switch to ${next}`}
            aria-label={`Density: ${density}. Switch to ${next}.`}
            aria-pressed={density === "compact"}
            onClick={() => setDensity(next)}>
      {density === "compact" ? "▤" : "▦"}
    </button>
  );
}
