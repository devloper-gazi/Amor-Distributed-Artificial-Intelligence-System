/**
 * Cycle C Sprint 4 Day 2 — pure parsers for the unified composer.
 *
 * Extracted to a dedicated module so unit tests don't have to spin
 * up a DOM (vitest's default ``node`` environment triggers solid-js'
 * "client-only API on server" guard the moment any UI primitive is
 * imported).  Both ``parseSlashCommand`` and ``detectMention`` are
 * pure functions on strings — no Solid signals, no DOM.
 */

import { type ModeKey, MODES } from "../../lib/types";

export const SLASH_ALIASES: Record<string, ModeKey> = {
  "/build": "build",
  "/code": "build",
  "/research": "research",
  "/think": "thinking",
  "/thinking": "thinking",
  "/consortium": "consortium",
  "/team": "consortium",
  "/sentinel": "sentinel",
  "/audit": "sentinel",
  "/system": "system",
  "/sys": "system",
  "/diag": "system",
};

export interface ParsedInput {
  text: string;
  mode: ModeKey;
  slashUsed: boolean;
}

export function parseSlashCommand(
  raw: string,
  activeMode: ModeKey,
): ParsedInput {
  const trimmed = raw.replace(/^\s+/, "");
  if (!trimmed.startsWith("/")) {
    return { text: raw.trim(), mode: activeMode, slashUsed: false };
  }
  const spaceIdx = trimmed.search(/\s/);
  const head = (spaceIdx === -1 ? trimmed : trimmed.slice(0, spaceIdx)).toLowerCase();
  const tail = spaceIdx === -1 ? "" : trimmed.slice(spaceIdx + 1);
  const mapped = SLASH_ALIASES[head];
  if (mapped) {
    return { text: tail.trim(), mode: mapped, slashUsed: true };
  }
  return { text: raw.trim(), mode: activeMode, slashUsed: false };
}

/**
 * Detect an active ``@<word>`` token at the caret position.  Returns
 * the substring after ``@`` (could be empty) and the start index of
 * the ``@`` itself, or null if there's no live mention.
 */
export function detectMention(
  raw: string,
  caret: number,
): { atIndex: number; query: string } | null {
  if (caret <= 0 || caret > raw.length) return null;
  let i = caret - 1;
  while (i >= 0) {
    const ch = raw[i];
    if (ch === undefined) return null;
    if (ch === "@") {
      const left = i === 0 ? "" : raw[i - 1];
      if (i === 0 || (left !== undefined && /\s/.test(left))) {
        const q = raw.slice(i + 1, caret);
        if (/^[\w.-]*$/.test(q)) {
          return { atIndex: i, query: q };
        }
        return null;
      }
      return null;
    }
    if (/\s/.test(ch)) return null;
    i -= 1;
  }
  return null;
}

/** A symbol entry returned by ``GET /api/repo/symbols``. */
export interface RepoSymbol {
  name: string;
  kind: string;
  path: string;
  line: number;
  end_line: number | null;
  parent: string | null;
  scope: string;
  /** Pre-formatted "@[name](path:line)" insertion token. */
  label: string;
}

/** Mode glyph mapping — matches Sidebar's text-glyph fallback so we
 *  don't pull in a real icon library yet. */
export const MODE_GLYPH: Record<ModeKey, string> = {
  research: "◎",
  build: "▲",
  thinking: "◊",
  consortium: "❖",
  sentinel: "◐",
  system: "≈",
};

/** Look up a mode's metadata, falling back to the first defined mode
 *  if the requested key is missing.  ``MODES`` is non-empty by
 *  construction, so this fallback satisfies strict-null checks
 *  without runtime cost. */
export function modeMeta(mode: ModeKey) {
  const found = MODES.find((m) => m.key === mode);
  return found ?? MODES[0]!;
}
