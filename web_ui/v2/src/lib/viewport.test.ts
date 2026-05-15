/**
 * Cycle C Sprint 11 Day 1 — viewport hook tests.
 *
 * Pure unit coverage for the ``classifyWidth`` / ``viewportSnapshot``
 * pair.  The reactive ``useViewport`` hook is exercised end-to-end
 * by Day 2's MobileShell rendering tests; today we just pin the
 * pure helpers + the breakpoint table.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  BREAKPOINTS,
  MOBILE_BREAKPOINT_PX,
  classifyWidth,
  viewportSnapshot,
} from "./viewport";


describe("BREAKPOINTS table", () => {
  it("matches Tailwind v4 defaults", () => {
    expect(BREAKPOINTS.xs).toBe(0);
    expect(BREAKPOINTS.sm).toBe(640);
    expect(BREAKPOINTS.md).toBe(768);
    expect(BREAKPOINTS.lg).toBe(1024);
    expect(BREAKPOINTS.xl).toBe(1280);
  });

  it("MOBILE_BREAKPOINT_PX equals md", () => {
    expect(MOBILE_BREAKPOINT_PX).toBe(BREAKPOINTS.md);
    expect(MOBILE_BREAKPOINT_PX).toBe(768);
  });
});


describe("classifyWidth()", () => {
  it("returns the correct breakpoint label for each band", () => {
    expect(classifyWidth(320)).toBe("xs");      // small phone
    expect(classifyWidth(639)).toBe("xs");      // just below sm
    expect(classifyWidth(640)).toBe("sm");      // sm boundary inclusive
    expect(classifyWidth(700)).toBe("sm");
    expect(classifyWidth(767)).toBe("sm");      // just below md
    expect(classifyWidth(768)).toBe("md");      // md boundary inclusive
    expect(classifyWidth(900)).toBe("md");
    expect(classifyWidth(1023)).toBe("md");
    expect(classifyWidth(1024)).toBe("lg");
    expect(classifyWidth(1279)).toBe("lg");
    expect(classifyWidth(1280)).toBe("xl");
    expect(classifyWidth(2560)).toBe("xl");
  });
});


// ─── viewportSnapshot ─────────────────────────────────────────


describe("viewportSnapshot()", () => {
  let originalInnerWidth: number;
  let originalInnerHeight: number;
  let originalVV: VisualViewport | undefined;

  beforeEach(() => {
    originalInnerWidth = window.innerWidth;
    originalInnerHeight = window.innerHeight;
    originalVV = window.visualViewport ?? undefined;
  });

  afterEach(() => {
    Object.defineProperty(window, "innerWidth",  { value: originalInnerWidth, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: originalInnerHeight, configurable: true });
    Object.defineProperty(window, "visualViewport", { value: originalVV, configurable: true });
  });

  it("classifies a desktop width as not-mobile", () => {
    Object.defineProperty(window, "innerWidth",  { value: 1280, configurable: true });
    Object.defineProperty(window, "innerHeight", { value:  800, configurable: true });
    const snap = viewportSnapshot();
    expect(snap.width).toBe(1280);
    expect(snap.height).toBe(800);
    expect(snap.isMobile).toBe(false);
    expect(snap.breakpoint).toBe("xl");
    expect(snap.keyboardOffset).toBe(0);
  });

  it("classifies sub-768 widths as mobile", () => {
    Object.defineProperty(window, "innerWidth",  { value: 390, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
    const snap = viewportSnapshot();
    expect(snap.isMobile).toBe(true);
    expect(snap.breakpoint).toBe("xs");
  });

  it("computes keyboardOffset from visualViewport delta", () => {
    Object.defineProperty(window, "innerWidth",  { value: 390, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
    // Simulate iOS Safari: keyboard up means visualViewport.height
    // shrinks by ~336 px.
    Object.defineProperty(window, "visualViewport", {
      value: { width: 390, height: 508, offsetTop: 0 },
      configurable: true,
    });
    const snap = viewportSnapshot();
    expect(snap.keyboardOffset).toBe(844 - 508);  // 336 px
  });

  it("clamps keyboardOffset to non-negative", () => {
    Object.defineProperty(window, "innerWidth",  { value: 390, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
    // Browser chrome change can briefly report visualViewport
    // *taller* than the layout viewport — the helper must clamp.
    Object.defineProperty(window, "visualViewport", {
      value: { width: 390, height: 900, offsetTop: 0 },
      configurable: true,
    });
    const snap = viewportSnapshot();
    expect(snap.keyboardOffset).toBe(0);
  });

  it("returns zero keyboardOffset when visualViewport is unavailable", () => {
    Object.defineProperty(window, "innerWidth",  { value: 800, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 600, configurable: true });
    Object.defineProperty(window, "visualViewport", { value: undefined, configurable: true });
    const snap = viewportSnapshot();
    expect(snap.keyboardOffset).toBe(0);
  });
});
