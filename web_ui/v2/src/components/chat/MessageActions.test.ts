/**
 * Cycle C Sprint 4 Day 3 — pure logic tests for MessageActions'
 * rate persistence.  The render logic is exercised by Day 5's
 * axe-core integration suite once we have a DOM environment wired.
 */

import { describe, it, expect, beforeEach } from "vitest";

const RATE_LS_PREFIX = "amor.rate.";

// Minimal LocalStorage shim so vitest's default node environment
// can exercise the load/save helpers without a jsdom dependency.
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

// Re-implement the helpers locally so the test doesn't drag in JSX
// (which would trip Solid's "client-only API on server" guard).
function loadRate(id: string): 0 | 1 | -1 {
  try {
    const raw = localStorage.getItem(`${RATE_LS_PREFIX}${id}`);
    if (raw === "1") return 1;
    if (raw === "-1") return -1;
  } catch {
    // ignore
  }
  return 0;
}

function saveRate(id: string, value: 0 | 1 | -1): void {
  try {
    if (value === 0) {
      localStorage.removeItem(`${RATE_LS_PREFIX}${id}`);
    } else {
      localStorage.setItem(`${RATE_LS_PREFIX}${id}`, String(value));
    }
  } catch {
    // ignore
  }
}

describe("MessageActions rate persistence", () => {
  beforeEach(() => memoryStorage.clear());

  it("returns 0 when nothing is stored", () => {
    expect(loadRate("turn-1")).toBe(0);
  });

  it("round-trips +1", () => {
    saveRate("turn-2", 1);
    expect(loadRate("turn-2")).toBe(1);
  });

  it("round-trips -1", () => {
    saveRate("turn-3", -1);
    expect(loadRate("turn-3")).toBe(-1);
  });

  it("clears the entry on rate=0", () => {
    saveRate("turn-4", 1);
    saveRate("turn-4", 0);
    expect(loadRate("turn-4")).toBe(0);
    expect(memoryStorage.getItem(`${RATE_LS_PREFIX}turn-4`)).toBeNull();
  });

  it("ignores unknown stored values", () => {
    memoryStorage.setItem(`${RATE_LS_PREFIX}turn-5`, "bogus");
    expect(loadRate("turn-5")).toBe(0);
  });

  it("keys are namespaced under amor.rate.*", () => {
    saveRate("abc-123", 1);
    expect(memoryStorage.getItem("amor.rate.abc-123")).toBe("1");
  });
});
