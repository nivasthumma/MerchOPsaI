import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";
const KEY = "merchantops.theme";

function read(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Cycles system → light → dark. "System" is the default rather than a light
 *  default, so the app matches whatever the operator's machine already does. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      if (theme === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, theme);
    } catch {
      /* the choice simply will not survive a reload */
    }
  }, [theme]);

  const next: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };
  const label = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" }[theme];

  return (
    <button className="icon-btn theme-btn" title={label} aria-label={label}
            onClick={() => setTheme(next[theme])}>
      <ThemeIcon theme={theme} />
    </button>
  );
}

/** Drawn rather than set in type.
 *
 *  These were the characters ☀ ☾ ◐. Two of them are outside the console's
 *  typefaces, so the browser substituted whatever it had: on this machine the
 *  "system" state rendered as a 6px sliver nobody could identify as anything.
 *  A control's only label should not depend on a glyph being present in a font
 *  the page never loaded.
 */
function ThemeIcon({ theme }: { theme: Theme }) {
  return (
    <svg viewBox="0 0 20 20" width="17" height="17" aria-hidden="true"
         fill="none" stroke="currentColor" strokeWidth="1.6"
         strokeLinecap="round" strokeLinejoin="round">
      {theme === "light" ? (
        <>
          <circle cx="10" cy="10" r="3.6" />
          {[0, 45, 90, 135, 180, 225, 270, 315].map((d) => (
            <line key={d} x1="10" y1="1.9" x2="10" y2="3.9"
                  transform={`rotate(${d} 10 10)`} />
          ))}
        </>
      ) : null}
      {theme === "dark" ? (
        <path d="M16.2 12.4A6.8 6.8 0 0 1 7.6 3.8a6.8 6.8 0 1 0 8.6 8.6z" />
      ) : null}
      {theme === "system" ? (
        /* Half filled: the page takes half its answer from the machine. */
        <>
          <circle cx="10" cy="10" r="6.8" />
          <path d="M10 3.2a6.8 6.8 0 0 0 0 13.6z" fill="currentColor" stroke="none" />
        </>
      ) : null}
    </svg>
  );
}
