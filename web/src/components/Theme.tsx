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
  const glyph = { system: "◐", light: "☀", dark: "☾" }[theme];

  return (
    <button className="icon-btn" title={label} aria-label={label}
            onClick={() => setTheme(next[theme])}>
      {glyph}
    </button>
  );
}
