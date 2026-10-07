import { useCallback, useEffect, useState } from "react";

import { getJson, type Params } from "./api";

// ============================================================
// DATA
// ============================================================

export interface Loaded<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
}

/**
 * GET a JSON endpoint; `path` null means nothing to fetch. While a new
 * request loads, the previous data stays, so views keep their frame
 * instead of flashing empty.
 */
export function useApi<T>(path: string | null, params: Params = {}): Loaded<T> {
  const key = path === null ? null : `${path} ${JSON.stringify(params)}`;
  const [state, setState] = useState<Loaded<T>>({ data: undefined, error: undefined, loading: key !== null });

  useEffect(() => {
    if (path === null) {
      setState({ data: undefined, error: undefined, loading: false });
      return;
    }

    const controller = new AbortController();
    setState((previous) => ({ ...previous, loading: true }));

    getJson<T>(path, params, controller.signal)
      .then((data) => setState({ data, error: undefined, loading: false }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ data: undefined, error: error instanceof Error ? error : new Error(String(error)), loading: false });
      });

    return () => controller.abort();
    // `key` stands for path and params
  }, [key]);

  return state;
}

// ============================================================
// ROUTE
//
// The page, the part and the as-of hour live in the URL hash
// (#/parts/272133?at=16000), so every view can be linked to.
// ============================================================

export type Page = "overview" | "map" | "stations" | "parts";

export interface Route {
  page: Page;
  partId: number | null;
  /** Production hour to show; null means the end of the data ("now") */
  at: number | null;
}

const PAGES: Page[] = ["overview", "map", "stations", "parts"];

export function parseHash(hash: string): Route {
  const [path, query = ""] = hash.replace(/^#\/?/, "").split("?");
  const [first, second] = path.split("/");

  const page = PAGES.includes(first as Page) ? (first as Page) : "overview";
  const partId = page === "parts" && second && /^\d+$/.test(second) ? Number(second) : null;

  const atText = new URLSearchParams(query).get("at");
  const at = atText !== null && atText !== "" && Number(atText) >= 0 ? Number(atText) : null;

  return { page, partId, at: at !== null && Number.isFinite(at) ? at : null };
}

export function toHash({ page, partId, at }: Route): string {
  const path = page === "parts" && partId !== null ? `parts/${partId}` : page;
  return `#/${path}${at === null ? "" : `?at=${at}`}`;
}

export function useRoute(): [Route, (next: Partial<Route>, options?: { replace?: boolean }) => void] {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));

  useEffect(() => {
    const sync = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, []);

  // Moving the time control replaces the history entry instead of adding one
  const navigate = useCallback((next: Partial<Route>, options: { replace?: boolean } = {}) => {
    const target = { ...parseHash(window.location.hash), ...next };
    const hash = toHash(target);

    if (hash !== window.location.hash) {
      if (options.replace) window.history.replaceState(null, "", hash);
      else window.history.pushState(null, "", hash);
    }
    setRoute(target);
  }, []);

  return [route, navigate];
}

// ============================================================
// THEME
// ============================================================

export type ThemeChoice = "system" | "light" | "dark";

const THEME_KEY = "forgeops-theme";

function storedTheme(): ThemeChoice {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function useTheme(): { choice: ThemeChoice; mode: "light" | "dark"; setChoice: (choice: ThemeChoice) => void } {
  const [choice, setChoiceState] = useState<ThemeChoice>(storedTheme);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches);

  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (choice === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", choice);
  }, [choice]);

  const setChoice = useCallback((next: ThemeChoice) => {
    setChoiceState(next);
    try {
      if (next === "system") window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, next);
    } catch {
      // Storage unavailable: the choice lasts for this visit only
    }
  }, []);

  return { choice, mode: choice === "system" ? (systemDark ? "dark" : "light") : choice, setChoice };
}
