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
