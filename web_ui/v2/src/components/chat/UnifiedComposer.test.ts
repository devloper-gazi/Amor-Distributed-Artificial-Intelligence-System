/**
 * Cycle C Sprint 4 Day 2 — pure-function tests for the slash + mention
 * parsers.  No DOM, no JSX.  Vitest only.
 *
 * Renders / interaction-level tests will land in Day 5 alongside the
 * axe-core a11y gate (where we already pull @testing-library/jest-dom
 * into the test runner).
 */

import { describe, it, expect } from "vitest";
import { parseSlashCommand, detectMention } from "./composer-parsers";

describe("parseSlashCommand", () => {
  it("falls back to active mode when no slash prefix", () => {
    const r = parseSlashCommand("hello world", "build");
    expect(r).toEqual({ text: "hello world", mode: "build", slashUsed: false });
  });

  it("recognises canonical /build", () => {
    const r = parseSlashCommand("/build a snake game", "research");
    expect(r.text).toBe("a snake game");
    expect(r.mode).toBe("build");
    expect(r.slashUsed).toBe(true);
  });

  it("normalises case", () => {
    const r = parseSlashCommand("/RESEARCH compare crdt", "build");
    expect(r.mode).toBe("research");
    expect(r.text).toBe("compare crdt");
  });

  it("expands /think → thinking", () => {
    const r = parseSlashCommand("/think trade-offs", "build");
    expect(r.mode).toBe("thinking");
    expect(r.text).toBe("trade-offs");
  });

  it("expands /sys → system", () => {
    const r = parseSlashCommand("/sys diagnostics", "build");
    expect(r.mode).toBe("system");
  });

  it("ignores unknown slash commands", () => {
    const r = parseSlashCommand("/foo do thing", "research");
    expect(r.slashUsed).toBe(false);
    expect(r.mode).toBe("research");
    expect(r.text).toBe("/foo do thing");
  });

  it("strips leading whitespace before recognising slash", () => {
    const r = parseSlashCommand("   /build a thing", "research");
    expect(r.mode).toBe("build");
    expect(r.slashUsed).toBe(true);
    expect(r.text).toBe("a thing");
  });

  it("handles slash with no body", () => {
    const r = parseSlashCommand("/build", "research");
    expect(r.mode).toBe("build");
    expect(r.text).toBe("");
  });
});

describe("detectMention", () => {
  it("returns null when no @ before caret", () => {
    expect(detectMention("hello world", 5)).toBeNull();
  });

  it("detects empty mention right after @", () => {
    const r = detectMention("hello @", 7);
    expect(r).toEqual({ atIndex: 6, query: "" });
  });

  it("captures partial query", () => {
    const r = detectMention("hello @Re", 9);
    expect(r).toEqual({ atIndex: 6, query: "Re" });
  });

  it("captures dotted module names", () => {
    const r = detectMention("@util.common", 12);
    expect(r).toEqual({ atIndex: 0, query: "util.common" });
  });

  it("ignores email addresses (no whitespace before @)", () => {
    const r = detectMention("user@host.com", 13);
    expect(r).toBeNull();
  });

  it("closes when whitespace appears between @ and caret", () => {
    const r = detectMention("hello @foo bar", 14);
    expect(r).toBeNull();
  });

  it("returns null for caret beyond text length", () => {
    expect(detectMention("hi", 99)).toBeNull();
  });

  it("returns null for caret at zero", () => {
    expect(detectMention("@x", 0)).toBeNull();
  });

  it("triggers on @ at start of input", () => {
    const r = detectMention("@", 1);
    expect(r).toEqual({ atIndex: 0, query: "" });
  });

  it("rejects mention containing invalid character", () => {
    // "@" followed by space then word — no live mention because the
    // walk-back stops at the whitespace.
    const r = detectMention("@ foo", 5);
    expect(r).toBeNull();
  });
});
