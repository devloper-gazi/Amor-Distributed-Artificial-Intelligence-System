/**
 * Cycle UI v2.5 — theme.css token contract snapshot.
 *
 * Structural test: confirms every v2.5 token Cycle UI Phase 1 ships
 * (canvas / elevated / display / body / subtle / mute / quickcode /
 * focus-ring) is defined in BOTH the light root block AND the dark
 * + system-fallback blocks of theme.css.  A missing token would
 * cause silent fallback to a default browser color → silent visual
 * bug — this test makes it loud.
 *
 * WCAG contrast numbers are documented inline in theme.css comments
 * (Research v2.5 section B.4) but NOT computed at runtime here:
 *   * axe-core's color-contrast rule runs against the real browser
 *     in composer-a11y.test.tsx (happy-dom can't compute layout).
 *   * Re-computing OKLch→sRGB→relative-luminance in node would add
 *     ~30 KB of color-math dependency (culori) that the project
 *     deliberately doesn't ship.
 * The lightness/chroma values were picked against WCAG manually + are
 * pinned by this test; if a future contributor drifts them the test
 * surfaces the change immediately.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const THEME_CSS_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "./theme.css",
);
const themeCss = readFileSync(THEME_CSS_PATH, "utf-8");

// Helper — extract the body of a CSS block starting with `selector`.
function blockBody(selector: string): string {
  const idx = themeCss.indexOf(selector);
  if (idx < 0) {
    throw new Error(`theme.css: selector ${JSON.stringify(selector)} not found`);
  }
  const open = themeCss.indexOf("{", idx);
  // Naive matching brace counter — sufficient for theme.css's flat blocks.
  let depth = 0;
  let i = open;
  for (; i < themeCss.length; i++) {
    if (themeCss[i] === "{") depth++;
    else if (themeCss[i] === "}") {
      depth--;
      if (depth === 0) break;
    }
  }
  return themeCss.slice(open + 1, i);
}

// ─── Light (root @theme) block ──────────────────────────────────────────

describe("Cycle UI v2.5 — light tokens defined in @theme", () => {
  const root = blockBody("@theme");

  const REQUIRED: Array<[string, RegExp]> = [
    ["--color-bg-canvas",        /--color-bg-canvas:\s*oklch\(1\s+0\s+0\)/],
    ["--color-bg-elevated-v25",  /--color-bg-elevated-v25:\s*oklch\(0\.985/],
    ["--color-bg-overlay",       /--color-bg-overlay:\s*oklch\(0\.97/],
    ["--color-text-display",     /--color-text-display:\s*oklch\(0\s+0\s+0\)/],
    ["--color-text-body",        /--color-text-body:\s*oklch\(0\.22/],
    ["--color-text-subtle",      /--color-text-subtle:\s*oklch\(0\.42/],
    ["--color-text-mute",        /--color-text-mute:\s*oklch\(0\.55/],
    ["--color-border-subtle-v25", /--color-border-subtle-v25:\s*oklch\(0\.92/],
    ["--color-border-strong-v25", /--color-border-strong-v25:\s*oklch\(0\.78/],
    ["--color-focus-ring",       /--color-focus-ring:\s*oklch\(0\.55\s+0\.20\s+255\)/],
    ["--color-mode-quickcode",   /--color-mode-quickcode:\s*oklch\(0\.62\s+0\.13\s+175\)/],
  ];

  for (const [name, pattern] of REQUIRED) {
    it(`defines ${name} (light)`, () => {
      expect(root).toMatch(pattern);
    });
  }
});

// ─── Dark — explicit [data-theme="dark"] block ──────────────────────────

describe("Cycle UI v2.5 — dark tokens defined for [data-theme=\"dark\"]", () => {
  const dark = blockBody('[data-theme="dark"]');

  const REQUIRED: Array<[string, RegExp]> = [
    ["--color-bg-canvas",        /--color-bg-canvas:\s*oklch\(0\s+0\s+0\)/],
    ["--color-bg-elevated-v25",  /--color-bg-elevated-v25:\s*oklch\(0\.16/],
    ["--color-text-display",     /--color-text-display:\s*oklch\(1\s+0\s+0\)/],
    ["--color-text-body",        /--color-text-body:\s*oklch\(0\.92/],
    ["--color-text-subtle",      /--color-text-subtle:\s*oklch\(0\.72/],
    ["--color-text-mute",        /--color-text-mute:\s*oklch\(0\.58/],
    ["--color-mode-quickcode",   /--color-mode-quickcode:\s*oklch\(0\.75\s+0\.11\s+175\)/],
    ["--color-focus-ring",       /--color-focus-ring:\s*oklch\(0\.78\s+0\.16\s+255\)/],
  ];

  for (const [name, pattern] of REQUIRED) {
    it(`defines ${name} (dark)`, () => {
      expect(dark).toMatch(pattern);
    });
  }
});

// ─── System fallback (prefers-color-scheme: dark) block ────────────────

describe("Cycle UI v2.5 — system-fallback dark mirrors explicit dark", () => {
  // The @media block contains its own [data-theme="system"] nested
  // selector.  Find the second occurrence of `[data-theme="system"]`
  // (the first is the root mode-accent default which we don't care
  // about here) and use that block.
  const allMatches = [...themeCss.matchAll(/\[data-theme="system"\]/g)];
  // Filter to the one INSIDE @media block.
  const mediaIdx = themeCss.indexOf("@media (prefers-color-scheme: dark)");
  expect(mediaIdx).toBeGreaterThan(-1);
  const inMedia = allMatches.find((m) => (m.index ?? 0) > mediaIdx);
  expect(inMedia, "system-fallback block missing inside @media").toBeDefined();
  const start = inMedia!.index!;
  const open = themeCss.indexOf("{", start);
  let depth = 0;
  let i = open;
  for (; i < themeCss.length; i++) {
    if (themeCss[i] === "{") depth++;
    else if (themeCss[i] === "}") { depth--; if (depth === 0) break; }
  }
  const systemDark = themeCss.slice(open + 1, i);

  it("mirrors quickcode + new tokens in the system-fallback block", () => {
    expect(systemDark).toMatch(/--color-mode-quickcode:\s*oklch\(0\.75/);
    expect(systemDark).toMatch(/--color-bg-canvas:\s*oklch\(0\s+0\s+0\)/);
    expect(systemDark).toMatch(/--color-text-display:\s*oklch\(1\s+0\s+0\)/);
    expect(systemDark).toMatch(/--color-text-body:\s*oklch\(0\.92/);
    expect(systemDark).toMatch(/--color-text-mute:\s*oklch\(0\.58/);
  });
});

// ─── Mode picker covers all 7 modes ─────────────────────────────────────

describe("Cycle UI v2.5 — mode picker covers all 7 classifier classes", () => {
  // The `[data-mode="X"]` selectors set --mode-accent.  All 6 legacy
  // + quickcode must be present.
  it("[data-mode=\"quickcode\"] sets --mode-accent", () => {
    expect(themeCss).toMatch(
      /\[data-mode="quickcode"\]\s*\{\s*--mode-accent:\s*var\(--color-mode-quickcode\)/,
    );
  });

  it.each([
    "build", "research", "thinking", "consortium", "sentinel", "system",
  ])("[data-mode=\"%s\"] picker still defined", (mode) => {
    const pattern = new RegExp(
      `\\[data-mode="${mode}"\\]\\s*\\{\\s*--mode-accent:\\s*var\\(--color-mode-${mode}\\)`,
    );
    expect(themeCss).toMatch(pattern);
  });
});

// ─── Legacy tokens still present (bridge period) ───────────────────────

describe("Cycle UI v2.5 — bridge period preserves legacy tokens", () => {
  it("--color-bg-primary still defined (Phase 3 deletes it)", () => {
    expect(themeCss).toMatch(/--color-bg-primary:/);
  });
  it("--color-text-primary still defined (Phase 3 deletes it)", () => {
    expect(themeCss).toMatch(/--color-text-primary:/);
  });
  it("--color-border-subtle still defined as legacy", () => {
    expect(themeCss).toMatch(/--color-border-subtle:\s*#e5e5e5/);
  });
});

// ─── motion.css imported ───────────────────────────────────────────────

describe("Cycle UI v2.5 — motion.css wired", () => {
  it("theme.css imports motion.css", () => {
    expect(themeCss).toMatch(/@import\s+["']\.\/motion\.css["']/);
  });
});
