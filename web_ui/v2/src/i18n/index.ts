/**
 * Cycle C Sprint 10 Day 1 — i18n primitive + Intl helpers.
 *
 * A 100-line homegrown translator + Solid signal.  Reasons we don't
 * pull in a library:
 *
 * * The whole AMOR string surface fits in two TS files (en.ts, tr.ts);
 *   adding a dep for that is overkill.
 * * Solid's signals are already the right reactive primitive — every
 *   consumer just calls ``t("some.key")`` and the UI re-renders when
 *   the locale flips.
 * * Smaller surface = lower bundle delta (Sprint 4 budget is +40 kB
 *   gzipped from baseline; Sprint 9 already used 5.6 kB of that).
 *
 * Public surface (used by routes + components):
 *
 *   import { t, locale, setLocale, formatDate, formatNumber,
 *            normalizeTurkish, useT } from "@/i18n";
 *
 * The ``locale`` signal is the source of truth.  ``setLocale("tr")``
 * persists to ``localStorage["amor.locale"]`` and triggers a UI
 * re-render via Solid's reactivity.
 *
 * Turkish caveat
 * --------------
 * Turkish has dotted/dotless i (i↔İ, ı↔I) — naive ``toLowerCase()``
 * mangles ``İ → i̇`` (combining dot above) on some browsers.
 * ``normalizeTurkish`` folds both variants to the dotless ``i``
 * baseline so search/sort works regardless of input variant.
 */

import { createSignal } from "solid-js";

import { en } from "./en";
import { tr } from "./tr";

export type Locale = "en" | "tr";

const LS_KEY = "amor.locale";
const SUPPORTED: ReadonlyArray<Locale> = ["en", "tr"];

const TABLES: Record<Locale, Record<string, string>> = {
  en,
  tr,
};

// ─── locale signal ───────────────────────────────────────────────


function loadLocale(): Locale {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw && SUPPORTED.includes(raw as Locale)) {
      return raw as Locale;
    }
    // Fall back to the browser locale's primary subtag.
    if (typeof navigator !== "undefined" && navigator.language) {
      const primary = navigator.language.split("-")[0]?.toLowerCase();
      if (primary && SUPPORTED.includes(primary as Locale)) {
        return primary as Locale;
      }
    }
  } catch {
    // ignore localStorage / SSR mismatches
  }
  return "en";
}

const [locale, setLocaleSignal] = createSignal<Locale>(loadLocale());

// Boot-time: mirror the resolved locale onto <html lang="…"> so that
// screen-readers and CSS :lang(...) selectors see the right language
// from the first paint.  Without this, the static lang attribute on
// index.html lies to assistive tech about the page's content language.
if (typeof document !== "undefined") {
  try {
    document.documentElement.setAttribute("lang", locale());
  } catch {
    // ignore (SSR, head-less render)
  }
}

export { locale };

/**
 * Locale-aware uppercase wrapper.  Browsers do NOT honour ``lang``
 * for CSS ``text-transform: uppercase`` — Chrome stays on Unicode
 * default casing which turns Turkish ``i`` into ``I`` instead of
 * ``İ``.  Call this helper in components that need uppercase TR
 * text and remove the CSS class.
 */
export function localeUpper(s: string): string {
  if (!s) return "";
  try {
    return s.toLocaleUpperCase(locale());
  } catch {
    return s.toUpperCase();
  }
}

export function setLocale(next: Locale): void {
  if (!SUPPORTED.includes(next)) return;
  setLocaleSignal(next);
  try {
    localStorage.setItem(LS_KEY, next);
    document.documentElement.setAttribute("lang", next);
  } catch {
    // ignore
  }
}

export function getSupportedLocales(): ReadonlyArray<Locale> {
  return SUPPORTED;
}


// ─── translator ──────────────────────────────────────────────────


/**
 * Look up ``key`` for the active locale.  Falls back to English
 * then to the literal key (so a missing translation is at least
 * visible to the developer in QA).
 *
 * Pass ``params`` to interpolate ``{{name}}`` placeholders.  Values
 * are coerced to strings; ``undefined``/``null`` render as the
 * empty string.
 */
export function t(
  key: string,
  params?: Record<string, string | number | boolean | undefined | null>,
): string {
  const table = TABLES[locale()];
  const fallback = TABLES.en;
  let template = table[key] ?? fallback[key] ?? key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      const value = v === undefined || v === null ? "" : String(v);
      template = template.split(`{{${k}}}`).join(value);
    }
  }
  return template;
}

/** Convenience hook for components — same as ``t`` but using a
 *  fresh closure so consumers can pass it down as a prop without
 *  worrying about Solid memoisation rules. */
export function useT() {
  return t;
}


// ─── pluralization (Intl.PluralRules) ────────────────────────────


export interface PluralForms {
  one: string;
  other: string;
  zero?: string;
  two?: string;
  few?: string;
  many?: string;
}

/**
 * Pick the right plural form for ``n`` in the active locale.  Uses
 * the browser's ``Intl.PluralRules`` so Turkish (which has only
 * ``other`` cardinality) and English (``one`` / ``other``) both work
 * without hand-rolled logic.
 */
export function plural(n: number, forms: PluralForms): string {
  const rules = new Intl.PluralRules(locale());
  const tag = rules.select(n);
  const tmpl = forms[tag as keyof PluralForms] ?? forms.other;
  return tmpl.split("{{n}}").join(String(n));
}


// ─── Intl wrappers ──────────────────────────────────────────────


/** Format an absolute date in the active locale's default style. */
export function formatDate(
  ts: number | string | Date,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" },
): string {
  const d = ts instanceof Date ? ts : new Date(ts);
  if (isNaN(d.getTime())) return "";
  try {
    return new Intl.DateTimeFormat(locale(), options).format(d);
  } catch {
    return d.toISOString();
  }
}

/** Format ``ts`` relative to ``now`` ("3 minutes ago" / "3 dakika önce"). */
export function formatRelative(
  ts: number | string | Date,
  now: number | Date = Date.now(),
): string {
  const target = ts instanceof Date ? ts.getTime() : new Date(ts).getTime();
  if (isNaN(target)) return "";
  const reference = now instanceof Date ? now.getTime() : now;
  const deltaMs = target - reference;
  const absSec = Math.abs(deltaMs) / 1000;
  const fmt = new Intl.RelativeTimeFormat(locale(), { numeric: "auto" });
  if (absSec < 60) return fmt.format(Math.round(deltaMs / 1000), "second");
  if (absSec < 3600) return fmt.format(Math.round(deltaMs / 60_000), "minute");
  if (absSec < 86400) return fmt.format(Math.round(deltaMs / 3_600_000), "hour");
  return fmt.format(Math.round(deltaMs / 86_400_000), "day");
}

/** Format ``n`` in the active locale.  Pass ``options`` for percent /
 *  currency / fractional digits. */
export function formatNumber(
  n: number,
  options: Intl.NumberFormatOptions = {},
): string {
  if (typeof n !== "number" || !isFinite(n)) return "";
  try {
    return new Intl.NumberFormat(locale(), options).format(n);
  } catch {
    return String(n);
  }
}


// ─── mode helpers (Sidebar / CommandPalette / UnifiedComposer) ──


/**
 * Look up the localised label for a mode.  Accepts either a raw key
 * (``"research"``) or any object with a ``key`` field (e.g. a
 * ``ModeMeta``).  Falls back to the object's pre-existing ``label``
 * for backwards compatibility — every call site upgrades at its
 * own pace.
 */
export function modeLabel(mode: { key: string; label?: string } | string): string {
  const k = typeof mode === "string" ? mode : mode.key;
  const fallback = typeof mode === "object" ? mode.label : undefined;
  const out = t(`mode.${k}.label`);
  return out === `mode.${k}.label` ? (fallback ?? k) : out;
}

export function modeSubtitle(mode: { key: string; subtitle?: string } | string): string {
  const k = typeof mode === "string" ? mode : mode.key;
  const fallback = typeof mode === "object" ? mode.subtitle : undefined;
  const out = t(`mode.${k}.subtitle`);
  return out === `mode.${k}.subtitle` ? (fallback ?? "") : out;
}


// ─── Turkish dotted-i normalization ─────────────────────────────


/**
 * Fold Turkish ``İ`` / ``I`` / ``ı`` / ``i`` to a single canonical
 * lowercase ``i`` so case-insensitive search works on both Turkish
 * and English input.
 *
 * Why this isn't ``s.toLocaleLowerCase("tr")``: that flips ``I`` to
 * ``ı``, which is right for *Turkish* output but wrong for
 * *normalisation*.  We want every variant to collapse to the same
 * baseline regardless of locale.
 */
export function normalizeTurkish(s: string): string {
  if (!s) return "";
  // Step 1: drop combining dots above (the U+0307 that Turkish
  // ``İ.toLowerCase()`` introduces).
  // Step 2: lowercase via en-US so ``İ`` → ``i`` straightforwardly.
  // Step 3: replace dotless ``ı`` with dotted ``i`` so search treats
  // them as equivalent.
  return s
    .normalize("NFD")
    .replace(/̇/g, "")
    .toLocaleLowerCase("en-US")
    .replace(/ı/g, "i");
}


// ─── tiny test hook ─────────────────────────────────────────────


/**
 * Reset the locale to the value persisted in localStorage / browser
 * preference.  Tests use this to undo an explicit ``setLocale``
 * without touching the persistence layer.
 */
export function resetLocale(): void {
  setLocaleSignal(loadLocale());
}
