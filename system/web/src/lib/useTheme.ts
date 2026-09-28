import { useEffect, useState } from "react";

type Theme = "dark" | "light";

/** App-wide theme state, synchronized with next-themes. */
const STORAGE_KEY = "theme";

function read(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light") return "light";
  } catch {
    /* ignore */
  }
  return "dark";
}

function apply(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* ignore */
  }
}

/** Returns [theme, toggle, themeClass] — add themeClass to .lm-root */
export function useTheme(): [Theme, () => void, string] {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    apply(theme);
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === "dark" || e.newValue === "light")) {
        setTheme(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [theme]);

  const toggle = () => setTheme((prev) => (prev === "dark" ? "light" : "dark"));

  const cls = theme === "light" ? "lm-light" : "lm-dark";

  return [theme, toggle, cls];
}
