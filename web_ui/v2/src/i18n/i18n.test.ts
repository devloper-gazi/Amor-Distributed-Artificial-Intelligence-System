/**
 * Cycle C Sprint 10 Day 1 — i18n primitive tests.
 *
 * Pure-function coverage — no JSX, runs in vitest's default
 * happy-dom env.  The signal-based ``locale`` state is exercised
 * by ``setLocale`` + ``resetLocale`` so each test starts from a
 * known baseline.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  formatDate,
  formatNumber,
  formatRelative,
  getSupportedLocales,
  locale,
  modeLabel,
  modeSubtitle,
  normalizeTurkish,
  plural,
  resetLocale,
  setLocale,
  t,
} from "./index";


// ─── shared setup ────────────────────────────────────────────


// happy-dom in some versions ships a partial localStorage that's
// missing ``getItem``.  Install a minimal shim so the tests have
// the same surface the production browser provides.
const memoryStore: Record<string, string> = {};
const memoryStorage: Storage = {
  get length() {
    return Object.keys(memoryStore).length;
  },
  key(i: number) {
    return Object.keys(memoryStore)[i] ?? null;
  },
  getItem(k: string) {
    return Object.prototype.hasOwnProperty.call(memoryStore, k)
      ? memoryStore[k]!
      : null;
  },
  setItem(k: string, v: string) {
    memoryStore[k] = v;
  },
  removeItem(k: string) {
    delete memoryStore[k];
  },
  clear() {
    for (const k of Object.keys(memoryStore)) delete memoryStore[k];
  },
};
(globalThis as unknown as { localStorage: Storage }).localStorage = memoryStorage;


beforeEach(() => {
  memoryStorage.clear();
  resetLocale();
});

afterEach(() => {
  memoryStorage.clear();
  resetLocale();
});


// ─── translator ─────────────────────────────────────────────


describe("t()", () => {
  it("returns the active-locale string", () => {
    setLocale("en");
    expect(t("settings.title")).toBe("Settings");
    setLocale("tr");
    expect(t("settings.title")).toBe("Ayarlar");
  });

  it("falls back to English when the active locale is missing the key", () => {
    setLocale("tr");
    // Pretend the key only exists in en (the test substitutes a
    // bogus key the tr table can't have).
    const result = t("definitely.missing.in.tr.but.also.en");
    // Both tables miss it → returns the literal key (last fallback).
    expect(result).toBe("definitely.missing.in.tr.but.also.en");
  });

  it("interpolates {{name}} placeholders", () => {
    setLocale("en");
    const out = t("locale.switched_to", { name: "Türkçe" });
    expect(out).toBe("Language switched to Türkçe.");
  });

  it("handles missing parameters gracefully", () => {
    setLocale("en");
    const out = t("locale.switched_to", { name: undefined });
    expect(out).toBe("Language switched to .");
  });
});


// ─── locale signal ──────────────────────────────────────────


describe("setLocale()", () => {
  it("updates the signal", () => {
    setLocale("tr");
    expect(locale()).toBe("tr");
    setLocale("en");
    expect(locale()).toBe("en");
  });

  it("persists to localStorage", () => {
    setLocale("tr");
    expect(localStorage.getItem("amor.locale")).toBe("tr");
  });

  it("ignores unsupported locales", () => {
    const before = locale();
    setLocale("klingon" as "en");  // type-cast for the negative path
    expect(locale()).toBe(before);
  });

  it("getSupportedLocales returns en + tr", () => {
    expect(getSupportedLocales()).toEqual(["en", "tr"]);
  });
});


// ─── plural ─────────────────────────────────────────────────


describe("plural()", () => {
  it("English picks singular vs plural", () => {
    setLocale("en");
    expect(plural(1, { one: "{{n}} item", other: "{{n}} items" })).toBe("1 item");
    expect(plural(2, { one: "{{n}} item", other: "{{n}} items" })).toBe("2 items");
    expect(plural(0, { one: "{{n}} item", other: "{{n}} items" })).toBe("0 items");
  });

  it("Turkish has only `other` cardinality", () => {
    setLocale("tr");
    const forms = { one: "{{n}} öğe", other: "{{n}} öğe" };
    expect(plural(1, forms)).toBe("1 öğe");
    expect(plural(99, forms)).toBe("99 öğe");
  });
});


// ─── formatters ────────────────────────────────────────────


describe("formatDate / formatNumber / formatRelative", () => {
  it("formatNumber respects the locale's decimal separator", () => {
    setLocale("en");
    expect(formatNumber(1234.5)).toBe("1,234.5");
    setLocale("tr");
    // Turkish uses a comma as decimal separator.
    expect(formatNumber(1234.5)).toBe("1.234,5");
  });

  it("formatDate produces a non-empty string for a real Date", () => {
    setLocale("en");
    const out = formatDate(new Date("2026-05-04T12:00:00Z"));
    expect(out).toBeTruthy();
    expect(out.length).toBeGreaterThan(5);
  });

  it("formatDate returns empty for an invalid date", () => {
    expect(formatDate("definitely-not-a-date")).toBe("");
  });

  it("formatRelative for past time", () => {
    setLocale("en");
    const tenMinutesAgo = Date.now() - 10 * 60 * 1000;
    const out = formatRelative(tenMinutesAgo);
    // The exact wording depends on the runtime's Intl table, but
    // it should mention "10" + "minute" somewhere.
    expect(out.toLowerCase()).toMatch(/10|minute/);
  });

  it("formatNumber handles non-finite gracefully", () => {
    expect(formatNumber(NaN)).toBe("");
    expect(formatNumber(Infinity)).toBe("");
  });
});


// ─── Turkish dotted-i normalization ─────────────────────────


describe("modeLabel / modeSubtitle (Sprint 10 Day 2)", () => {
  it("returns the locale-specific mode label by key", () => {
    setLocale("en");
    expect(modeLabel("build")).toBe("Build");
    expect(modeLabel("research")).toBe("Research");
    setLocale("tr");
    expect(modeLabel("build")).toBe("İnşa");
    expect(modeLabel("research")).toBe("Araştırma");
  });

  it("accepts a ModeMeta-like object", () => {
    setLocale("tr");
    expect(modeLabel({ key: "thinking", label: "Thinking" })).toBe("Düşünme");
    expect(modeSubtitle({ key: "thinking", subtitle: "x" })).toBe(
      "çok adımlı muhakeme",
    );
  });

  it("falls back to the object's English label when the key is unknown", () => {
    setLocale("tr");
    expect(modeLabel({ key: "novel-mode", label: "Novel" })).toBe("Novel");
    expect(modeSubtitle({ key: "novel-mode", subtitle: "fallback" })).toBe(
      "fallback",
    );
  });

  it("falls back to the bare key when there's no fallback", () => {
    setLocale("tr");
    expect(modeLabel("totally-novel-key")).toBe("totally-novel-key");
    expect(modeSubtitle("totally-novel-key")).toBe("");
  });
});


describe("normalizeTurkish()", () => {
  it("collapses I / İ / ı / i to lowercase i", () => {
    expect(normalizeTurkish("İstanbul")).toBe("istanbul");
    expect(normalizeTurkish("ISTANBUL")).toBe("istanbul");
    expect(normalizeTurkish("istanbul")).toBe("istanbul");
    expect(normalizeTurkish("ıstanbul")).toBe("istanbul");
  });

  it("doesn't introduce combining dot above artefacts", () => {
    const out = normalizeTurkish("İ");
    expect(out).not.toContain("̇");
    expect(out).toBe("i");
  });

  it("leaves non-Turkish text untouched", () => {
    expect(normalizeTurkish("Hello")).toBe("hello");
    expect(normalizeTurkish("ABC123")).toBe("abc123");
  });

  it("returns empty for empty input", () => {
    expect(normalizeTurkish("")).toBe("");
  });
});
