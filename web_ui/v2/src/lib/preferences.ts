/**
 * Cycle UI v2.8.7 — User preferences singleton.
 *
 * Cross-component reactive store for user-tunable settings.  Two-tier
 * backing:
 *   (1) localStorage — instant read, single-device, default fallback
 *   (2) Backend `/api/preferences` — optional persist across devices.
 *       On mount we GET the server copy; if it differs from
 *       localStorage we merge (server wins for the keys it returned).
 *       PATCH on change writes back to the server.
 *
 * Initial pref: `auto_mode` (default true) — when ON the composer's
 * mode pill is informational only + the user can't manually override
 * via ModePicker.  All routing comes from the v2.8.5 heuristic +
 * v2.8.6 submit-time check.  When OFF (legacy mode) the pill is
 * clickable + the picker opens like in v2.6.
 *
 * Future prefs can extend Preferences interface; localStorage key is
 * versioned (`amor.prefs.v1`) so a schema change can ship without
 * stomping the user's old settings.
 */

import { createSignal, type Accessor } from "solid-js";
import { api } from "./api";

export interface Preferences {
  /** Auto-mode toggle.  When true, classifier picks the mode AND
   *  applies it on submit; user can't manually override (ModePicker
   *  is locked, slash commands still work as an escape hatch).
   *  Default: true. */
  auto_mode: boolean;
}

const STORAGE_KEY = "amor.prefs.v1";

const DEFAULTS: Preferences = {
  auto_mode: true,
};


function loadFromStorage(): Preferences {
  if (typeof localStorage === "undefined") return { ...DEFAULTS };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw);
    return { ...DEFAULTS, ...parsed };
  } catch {
    return { ...DEFAULTS };
  }
}

function saveToStorage(p: Preferences): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p));
  } catch {
    /* quota / disabled */
  }
}


// Singleton signal — every component that calls `prefs()` reads from
// the same source.  Survives route changes within an SPA session.
const [prefs, setPrefs] = createSignal<Preferences>(loadFromStorage());

let serverPullAttempted = false;


/** Reactive accessor — re-renders on any preference change. */
export const preferences: Accessor<Preferences> = prefs;


/** Update one or more preferences.  Saves to localStorage + tries
 *  to PATCH the backend (best-effort; auth failures or 404 are
 *  silent — single-device users still work). */
export function updatePreferences(partial: Partial<Preferences>): void {
  const next: Preferences = { ...prefs(), ...partial };
  setPrefs(next);
  saveToStorage(next);
  // Best-effort backend write — fire-and-forget.
  if (typeof window !== "undefined") {
    void api
      .patch<Preferences>("/api/preferences", partial)
      .catch(() => {
        /* network blip / not authed — localStorage still has the value */
      });
  }
}


/** Best-effort load of server-side preferences after auth.  Called
 *  once on first mount of a top-level page (UnifiedChat or Settings).
 *  Server values win for keys present in the response; missing keys
 *  keep their localStorage / default value. */
export async function syncFromServer(): Promise<void> {
  if (serverPullAttempted) return;
  serverPullAttempted = true;
  try {
    const server = await api.get<Partial<Preferences>>("/api/preferences");
    if (!server || typeof server !== "object") return;
    const next: Preferences = { ...prefs(), ...server };
    setPrefs(next);
    saveToStorage(next);
  } catch {
    /* not authed / endpoint missing — keep local */
  }
}


/** Convenience accessor for the most-used flag. */
export function isAutoMode(): boolean {
  return prefs().auto_mode === true;
}
