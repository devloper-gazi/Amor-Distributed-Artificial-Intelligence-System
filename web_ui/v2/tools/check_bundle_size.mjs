#!/usr/bin/env node
/**
 * Cycle C Sprint 4 Day 5 — bundle-size delta gate.
 *
 * Sums the gzipped size of every ``dist/assets/*.js`` file from a
 * fresh ``npm run build`` and compares it against the baseline at
 * ``tools/bundle-baseline.json``.  Fails (exit 1) if the delta
 * exceeds the allowed budget.
 *
 * Usage
 * -----
 *
 *     node tools/check_bundle_size.mjs              # gate
 *     node tools/check_bundle_size.mjs --update     # rewrite baseline
 *
 * Baseline schema (``tools/bundle-baseline.json``):
 *
 *     {
 *       "schema_version": 1,
 *       "captured_at_iso": "2026-05-04T...",
 *       "total_js_gz_bytes": 87123,
 *       "max_growth_bytes": 40960,
 *       "files": { "index.<hash>.js": 81920, ... }
 *     }
 *
 * Why gzipped, not raw
 * --------------------
 * The wire cost of the SPA is what the user actually pays.  Vite
 * already prints gzipped sizes, so the baseline is in the same units.
 */

import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { gzipSync } from "node:zlib";
import { resolve } from "node:path";
import { argv, exit, cwd } from "node:process";

const ROOT = cwd();
const ASSETS_DIR = resolve(ROOT, "dist", "assets");
const BASELINE_PATH = resolve(ROOT, "tools", "bundle-baseline.json");
const DEFAULT_BUDGET_BYTES = 40 * 1024; // +40 KB per Sprint 4 spec.

async function listJsAssets() {
  let entries;
  try {
    entries = await readdir(ASSETS_DIR);
  } catch (err) {
    console.error(
      `[bundle-size] dist/assets not found at ${ASSETS_DIR} — run \`npm run build\` first.`,
    );
    exit(2);
  }
  return entries.filter((f) => f.endsWith(".js"));
}

async function gzippedSize(file) {
  const path = resolve(ASSETS_DIR, file);
  const data = await readFile(path);
  return gzipSync(data, { level: 9 }).byteLength;
}

async function readBaseline() {
  try {
    const raw = await readFile(BASELINE_PATH, "utf8");
    const j = JSON.parse(raw);
    if (typeof j.total_js_gz_bytes !== "number") {
      throw new Error("missing total_js_gz_bytes");
    }
    return j;
  } catch (err) {
    return null;
  }
}

async function writeBaseline(payload) {
  await writeFile(BASELINE_PATH, JSON.stringify(payload, null, 2) + "\n");
}

function fmt(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} kB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

async function main() {
  const update = argv.includes("--update");
  const files = await listJsAssets();
  const sizeByFile = {};
  let total = 0;
  for (const f of files) {
    const sz = await gzippedSize(f);
    sizeByFile[f] = sz;
    total += sz;
  }

  const sortedFiles = Object.fromEntries(
    Object.entries(sizeByFile).sort(([a], [b]) => a.localeCompare(b)),
  );

  if (update) {
    const next = {
      schema_version: 1,
      captured_at_iso: new Date().toISOString(),
      total_js_gz_bytes: total,
      max_growth_bytes: DEFAULT_BUDGET_BYTES,
      files: sortedFiles,
    };
    await writeBaseline(next);
    console.log(`[bundle-size] baseline updated: total ${fmt(total)} (${total} B)`);
    exit(0);
  }

  const base = await readBaseline();
  if (!base) {
    console.warn(
      `[bundle-size] no baseline at ${BASELINE_PATH} — run with --update to capture one.`,
    );
    console.log(`[bundle-size] current total: ${fmt(total)} (${total} B)`);
    exit(0); // soft-pass on first run.
  }

  const budget =
    typeof base.max_growth_bytes === "number"
      ? base.max_growth_bytes
      : DEFAULT_BUDGET_BYTES;
  const delta = total - base.total_js_gz_bytes;

  console.log(
    `[bundle-size] baseline: ${fmt(base.total_js_gz_bytes)}  current: ${fmt(total)}  delta: ${delta >= 0 ? "+" : ""}${fmt(delta)} (budget: +${fmt(budget)})`,
  );

  if (delta > budget) {
    console.error(
      `[bundle-size] FAIL — bundle grew by ${fmt(delta)}, exceeds budget of +${fmt(budget)}.`,
    );
    console.error(
      `If this growth is intentional, re-run \`node tools/check_bundle_size.mjs --update\` and commit the new baseline.`,
    );
    exit(1);
  }

  console.log("[bundle-size] OK");
  exit(0);
}

main().catch((err) => {
  console.error(`[bundle-size] crashed: ${err.message}`);
  exit(2);
});
