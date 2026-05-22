/**
 * Cycle D — Sessions list polish tests.
 *
 * Drives the user-reported bug: "Sessions kısmı hatalı ve hangi
 * session aktif hangisi tamamlanmış veyahut iptal edilmiş vb."
 *
 * The new SessionList exposes pure helpers we can test without
 * mounting the component:
 *   - deriveActivityStatus  (pinned/archived/active/recent/idle/stale)
 *   - relativeTime          (localized, plural-safe)
 *
 * Component-level tests (groupSessions, currentMode highlight)
 * happen at the integration level via Solid's testing-library; this
 * file covers the pure helpers exhaustively.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  deriveActivityStatus,
  relativeTime,
  groupSessions,
  matchSession,
} from "./SessionList";
import { setLocale } from "../../i18n";
import type { ChatSession } from "../../lib/sessions";


// happy-dom localStorage shim (matches existing pattern)
const memoryStore: Record<string, string> = {};
const memoryStorage: Storage = {
  get length() { return Object.keys(memoryStore).length; },
  key(i: number) { return Object.keys(memoryStore)[i] ?? null; },
  getItem(k: string) {
    return Object.prototype.hasOwnProperty.call(memoryStore, k)
      ? memoryStore[k]! : null;
  },
  setItem(k: string, v: string) { memoryStore[k] = v; },
  removeItem(k: string) { delete memoryStore[k]; },
  clear() { for (const k of Object.keys(memoryStore)) delete memoryStore[k]; },
};
(globalThis as unknown as { localStorage: Storage }).localStorage = memoryStorage;


function s(overrides: Partial<ChatSession>): ChatSession {
  return {
    id: "test-id",
    mode: "research",
    title: "Test session",
    ...overrides,
  };
}

const NOW = new Date("2026-05-08T12:00:00Z").getTime();


// ─── deriveActivityStatus ─────────────────────────────────────────


describe("deriveActivityStatus()", () => {
  it("returns 'pinned' when pinned is true (overrides everything)", () => {
    const session = s({ pinned: true, archived: true, updated_at: "2025-01-01T00:00:00Z" });
    expect(deriveActivityStatus(session, NOW)).toBe("pinned");
  });

  it("returns 'archived' when archived (and not pinned)", () => {
    const session = s({ archived: true, updated_at: new Date(NOW - 30_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("archived");
  });

  it("returns 'active' for sessions updated < 60 s ago", () => {
    const session = s({ updated_at: new Date(NOW - 30_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("active");
  });

  it("returns 'recent' for sessions updated 1m–1h ago", () => {
    const session = s({ updated_at: new Date(NOW - 30 * 60_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("recent");
  });

  it("returns 'idle' for sessions updated 1h–24h ago", () => {
    const session = s({ updated_at: new Date(NOW - 5 * 60 * 60_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("idle");
  });

  it("returns 'stale' for sessions updated >24h ago", () => {
    const session = s({ updated_at: new Date(NOW - 5 * 24 * 60 * 60_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("stale");
  });

  it("returns 'stale' for sessions with missing/invalid updated_at", () => {
    expect(deriveActivityStatus(s({}), NOW)).toBe("stale");
    expect(deriveActivityStatus(s({ updated_at: "not-a-date" }), NOW)).toBe("stale");
  });

  it("falls back to created_at when updated_at is missing", () => {
    const session = s({ created_at: new Date(NOW - 10_000).toISOString() });
    expect(deriveActivityStatus(session, NOW)).toBe("active");
  });

  it("treats pinned + archived as pinned (pinned wins)", () => {
    const session = s({ pinned: true, archived: true });
    expect(deriveActivityStatus(session, NOW)).toBe("pinned");
  });
});


// ─── relativeTime ─────────────────────────────────────────────────


describe("relativeTime()", () => {
  beforeEach(() => {
    setLocale("en");
  });

  it("returns empty for missing input", () => {
    expect(relativeTime(undefined, NOW)).toBe("");
    expect(relativeTime("not-a-date", NOW)).toBe("");
  });

  it("renders 'just now' under 30 s", () => {
    const ts = new Date(NOW - 10_000).toISOString();
    expect(relativeTime(ts, NOW)).toMatch(/just now|az önce/i);
  });

  it("renders seconds for 30–60 s", () => {
    const ts = new Date(NOW - 45_000).toISOString();
    const out = relativeTime(ts, NOW);
    expect(out).toMatch(/45/);
  });

  it("renders minutes for 1m–60m", () => {
    const ts = new Date(NOW - 5 * 60_000).toISOString();
    const out = relativeTime(ts, NOW);
    expect(out).toMatch(/5/);
  });

  it("renders hours for 1h–24h", () => {
    const ts = new Date(NOW - 3 * 60 * 60_000).toISOString();
    const out = relativeTime(ts, NOW);
    expect(out).toMatch(/3/);
  });

  it("renders days for 1d–14d", () => {
    const ts = new Date(NOW - 7 * 24 * 60 * 60_000).toISOString();
    const out = relativeTime(ts, NOW);
    expect(out).toMatch(/7/);
  });

  it("falls back to a localized date for >14 d", () => {
    const ts = new Date(NOW - 30 * 24 * 60 * 60_000).toISOString();
    const out = relativeTime(ts, NOW);
    // toLocaleDateString output — just assert it's non-empty & doesn't
    // contain "ago" / "önce"
    expect(out.length).toBeGreaterThan(0);
    expect(out).not.toMatch(/ago|önce/);
  });

  it("respects the active locale (Turkish)", () => {
    setLocale("tr");
    const tsActive = new Date(NOW - 10_000).toISOString();
    expect(relativeTime(tsActive, NOW)).toBe("az önce");
    const tsMin = new Date(NOW - 5 * 60_000).toISOString();
    expect(relativeTime(tsMin, NOW)).toBe("5dk önce");
    const tsHr = new Date(NOW - 3 * 60 * 60_000).toISOString();
    expect(relativeTime(tsHr, NOW)).toBe("3sa önce");
    const tsDay = new Date(NOW - 2 * 24 * 60 * 60_000).toISOString();
    expect(relativeTime(tsDay, NOW)).toBe("2g önce");
    setLocale("en");
  });
});


// ─── groupSessions (Cycle UI v2.8.3 — refined taxonomy) ──────────


describe("groupSessions() — v2.8.3 refined recency", () => {
  const DAY = 24 * 60 * 60_000;

  function group(items: ChatSession[]) {
    return groupSessions(items, NOW);
  }

  it("returns empty array for empty input", () => {
    expect(group([])).toEqual([]);
  });

  it("places pinned session at the top regardless of age", () => {
    const out = group([
      s({ id: "p", pinned: true, updated_at: new Date(NOW - 60 * DAY).toISOString() }),
      s({ id: "t", updated_at: new Date(NOW - 1000).toISOString() }),
    ]);
    expect(out[0]?.key).toBe("pinned");
    expect(out[0]?.items[0]?.id).toBe("p");
  });

  it("splits today vs yesterday using a 24h cutoff", () => {
    const out = group([
      s({ id: "today", updated_at: new Date(NOW - 6 * 60 * 60_000).toISOString() }),
      s({ id: "yest",  updated_at: new Date(NOW - 30 * 60 * 60_000).toISOString() }),
    ]);
    const keys = out.map((g) => g.key);
    expect(keys).toContain("today");
    expect(keys).toContain("yesterday");
    const today = out.find((g) => g.key === "today");
    const yest = out.find((g) => g.key === "yesterday");
    expect(today?.items[0]?.id).toBe("today");
    expect(yest?.items[0]?.id).toBe("yest");
  });

  it("routes 3-7 day old sessions into 'past_week'", () => {
    const out = group([
      s({ id: "5d", updated_at: new Date(NOW - 5 * DAY).toISOString() }),
    ]);
    expect(out[0]?.key).toBe("past_week");
  });

  it("routes 8-30 day old sessions into 'past_month'", () => {
    const out = group([
      s({ id: "15d", updated_at: new Date(NOW - 15 * DAY).toISOString() }),
    ]);
    expect(out[0]?.key).toBe("past_month");
  });

  it("routes sessions >30 days old into 'older'", () => {
    const out = group([
      s({ id: "60d", updated_at: new Date(NOW - 60 * DAY).toISOString() }),
    ]);
    expect(out[0]?.key).toBe("older");
  });

  it("separates archived sessions into their own group", () => {
    const out = group([
      s({ id: "a", archived: true, updated_at: new Date(NOW - 1000).toISOString() }),
      s({ id: "t", updated_at: new Date(NOW - 1000).toISOString() }),
    ]);
    const keys = out.map((g) => g.key);
    expect(keys).toContain("archived");
    expect(keys).toContain("today");
  });

  it("preserves descending recency within a group", () => {
    const out = group([
      s({ id: "older", updated_at: new Date(NOW - 12 * 60 * 60_000).toISOString() }),
      s({ id: "newer", updated_at: new Date(NOW - 1 * 60 * 60_000).toISOString() }),
    ]);
    const today = out.find((g) => g.key === "today");
    expect(today?.items.map((x) => x.id)).toEqual(["newer", "older"]);
  });
});


// ─── matchSession (Cycle UI v2.8.3 — inline search filter) ───────


describe("matchSession() — v2.8.3 inline filter", () => {
  it("returns true for empty query (no filtering)", () => {
    expect(matchSession(s({ title: "Anything" }), "")).toBe(true);
  });

  it("matches by partial title (case-insensitive)", () => {
    expect(matchSession(s({ title: "Snake Game Design" }), "snake")).toBe(true);
    expect(matchSession(s({ title: "Snake Game Design" }), "SNAKE")).toBe(true);
    expect(matchSession(s({ title: "Snake Game Design" }), "game")).toBe(true);
  });

  it("returns false when no field matches", () => {
    expect(matchSession(s({ title: "Snake Game", mode: "build" }), "xyz")).toBe(false);
  });

  it("matches by mode key", () => {
    expect(matchSession(s({ title: "X", mode: "research" }), "research")).toBe(true);
    expect(matchSession(s({ title: "X", mode: "research" }), "RES")).toBe(true);
  });

  it("matches by id prefix (CLI-style lookup)", () => {
    expect(matchSession(s({ id: "abc123-foo", title: "X" }), "abc12")).toBe(true);
    expect(matchSession(s({ id: "abc123-foo", title: "X" }), "ABC")).toBe(true);
  });

  it("does not match by id substring (only prefix)", () => {
    expect(matchSession(s({ id: "abc123-foo", title: "X" }), "23-fo")).toBe(false);
  });

  it("handles missing title / mode gracefully", () => {
    expect(matchSession(s({ id: "test-id" }), "test")).toBe(true);
    expect(matchSession(s({ id: "test-id" }), "nope")).toBe(false);
  });
});
