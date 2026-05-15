# Sprint 12 — PWA service worker + Tauri 2.0 spike

> Cycle C, Days 1–3 (compressed from 5 — the spike's measurement
> phase is operator-driven and lands offline).  Closed 2026-05-04.
> **This is the last sprint of Cycle C.**

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | Hand-rolled service worker + `manifest.webmanifest` + 192/512 SVG icons; FastAPI routes `/sw.js`, `/manifest.webmanifest`, `/icon-{192,512}.svg`; SW registration helper at boot | `web_ui/v2/public/{sw.js,manifest.webmanifest,icon-*.svg}`, `web_ui/v2/src/lib/pwa.ts`, `document_processor/main.py` (4 PWA routes), 12 vitest |
| 2 | Tauri 2.0 spike scaffold — `Cargo.toml`, `tauri.conf.json` (window + bundle + CSP + WiX i18n), `src/main.rs`, runbook | `desktop/tauri/{Cargo.toml,tauri.conf.json,src/main.rs,README.md,.gitignore}` |
| 3 | Cross-sprint sweep + `sprint12_results.md` + `cycle_c_complete.md` | this file + `docs/cycle_c_complete.md` |

## Acceptance criteria — pass/fail

* **Service worker caches the SolidJS shell, fonts, and assets** —
  **PASS**.  `sw.js` pre-caches `/`, `/index.html`,
  `/manifest.webmanifest`, `/icon-{192,512}.svg` on `install`;
  cache-first for static assets / navigations; network-only for
  `/api/*` / `/v1/*` / `/mcp/*` so SSE + streaming never hit a
  stale cached response.

* **`manifest.webmanifest` with installable PWA metadata** —
  **PASS** (`name`, `short_name`, `start_url`, `scope`, `display`,
  `display_override`, `theme_color`, `categories`, 3
  `shortcuts` for Chat / Build / Agent, 192 + 512 px icons with
  `purpose: "any maskable"`).

* **SSE / streaming requires online** — **PASS** by design — the
  service worker's `/api/*` rule is `return;` (let the browser run
  the default fetch).  Cached responses are never served for
  these paths.

* **Tauri 2.0 spike (not shipped)** — **PASS as scaffold**.  The
  config + runbook land; the build itself is operator-driven so
  measurement happens on the operator's actual Windows machine
  rather than a CI surrogate.

* **Decision rationale captured** — **PASS** (see
  "Tauri vs PWA decision" section in `cycle_c_complete.md`).

## Frontend surface

* `web_ui/v2/public/sw.js` (~80 LOC) — hand-rolled service worker:
  * pre-cache shell on install
  * drop stale caches on activate
  * cache-first for `mode: "navigate"` with background refresh
  * cache-first for static assets, same-origin only
  * network-only for `/api/`, `/v1/`, `/mcp/`

* `web_ui/v2/public/manifest.webmanifest` — installable PWA
  metadata (matches MDN's "minimum viable manifest" + extras).

* `web_ui/v2/public/icon-{192,512}.svg` — minimal A-glyph icons,
  inline SVG (no PNG raster pipeline; SVG is universally supported
  in modern PWA installers).

* `web_ui/v2/src/lib/pwa.ts` (~110 LOC) — registration helper:
  * `serviceWorkerSupported()` — feature detect
  * `isProductionLike()` — Vite env signal so dev HMR never sees
    a SW
  * `pwaForceDisabled()` — `localStorage["amor.pwa"]==="off"`
    operator override
  * `registerServiceWorker()` — wires it all + logs failures
  * `unregisterServiceWorker()` — cleanup helper

* `index.html` — added `<link rel="manifest">` + `<link rel="icon">`
  + `<link rel="apple-touch-icon">`.

* `main.tsx` — calls `registerServiceWorker()` at boot.

## Backend surface

* `document_processor/main.py` — 4 explicit `@app.add_api_route`
  registrations for `/sw.js`, `/manifest.webmanifest`,
  `/icon-{192,512}.svg`, served from `_v2_dist_path` with proper
  MIME types (`application/manifest+json`, `application/javascript`,
  `image/svg+xml`).  Registered BEFORE the SPA catch-all so the
  catch-all doesn't intercept them.

## Tauri scaffold

```
desktop/tauri/
├── Cargo.toml          # release profile = size-opt (lto + opt-level "z")
├── tauri.conf.json     # window + bundle + CSP + WiX i18n (en-US, tr-TR)
├── src/main.rs         # ~10-line entrypoint
├── README.md           # build runbook + measurements table
└── .gitignore          # target/, signing keys
```

* CSP: `default-src 'self'; connect-src 'self' http://localhost:8000`
  so the desktop shell talks to the same FastAPI backend the PWA
  uses.
* Bundle targets: MSI + NSIS (Windows-first per Cycle C scope).
* Build runbook in `desktop/tauri/README.md` — operator runs
  `cargo tauri build` themselves; CI doesn't touch it.

## Tests

```
$ npx vitest run
Tests: 99 passed (was 87 → +12 PWA helper tests)

$ pytest tests/local_ai/ tests/api/ tests/code_intelligence/test_sandbox_security_posture.py tests/training/
158 passed
```

Cross-sprint backend sweep: **158 passed** (unchanged from Sprint
10 — Sprint 11 + 12 don't touch backend test surface beyond what
Sprint 10 already did).

Frontend sweep: **99 passed** (was 87 → +12 pwa.ts tests).

New PWA tests pin:
* `serviceWorkerSupported` feature detect (with/without
  `navigator.serviceWorker`)
* `isProductionLike` env signal
* `pwaForceDisabled` LS override (off / on / unset)
* `registerServiceWorker` four guarded paths (no SW / dev mode /
  operator-disabled / production happy path) + error swallow
* `unregisterServiceWorker` walks every registration

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 109.08 kB  delta: +12.88 kB (budget: +40.00 kB)
[bundle-size] OK
```

Cycle C cumulative delta is **+12.88 kB / +40 kB budget** (32%
used; 68% headroom).  PWA artefacts themselves (`sw.js` ≈ 2.5 kB,
`manifest.webmanifest` ≈ 1 kB, icons ≈ 600 B each) live OUTSIDE
the JS chunks and aren't counted against the budget — they're
served as separate files anyway.

## Live verification

```
$ curl -s http://localhost:8000/manifest.webmanifest | head -2
{
  "name": "AMOR — local-first distributed AI",

$ curl -s http://localhost:8000/sw.js | head -1
/**

$ curl -s -o /dev/null -w "%{content_type}\n" http://localhost:8000/icon-192.svg
image/svg+xml
```

DevTools Application panel:
- Manifest: validates with no warnings, 192/512 icons render
- Service Workers: `sw.js` activated, scope `/`, no errors
- "Install AMOR" prompt offered after the manifest + SW criteria
  are met (user gesture required per browser rules)

## Caveats

* **No raster icons**.  Browsers accept SVG icons in `manifest`
  for installation but some app stores (Microsoft Store,
  Chrome Web Store) want PNG fallbacks.  Out of scope for this
  sprint; trivial `svgexport` invocation when needed.
* **No offline chat history**.  The Cycle C plan mentions "last
  50 messages per chat for offline-read".  The chat history is
  already persisted in `localStorage["amor.chat.v1.<mode>.turns"]`
  (Sprint 4 Day 1) — that **already works offline** for the modes
  using it.  Wiring the SW to expose a richer offline read mode
  (e.g. via IndexedDB) is a follow-up sprint when there's a
  measured user need.
* **Push notifications**.  Manifest ships notification-capable
  metadata but no actual push integration.  AMOR is a local-first
  desktop app; push channels are an opt-in operator add-on.
* **Tauri build is offline-only** for now — requires Rust
  toolchain + tauri-cli on the operator's machine.  That's
  intentional: the Cycle C plan calls for a *spike*, not a
  release.  CI doesn't build Windows installers today.

## Rollback

* **Disable PWA install**: remove `<link rel="manifest">` from
  `index.html`.  The browser drops the install affordance; the
  service worker keeps running until users clear it.
* **Disable service worker**: have users run
  `localStorage.setItem("amor.pwa", "off")` then hard-reload.
  `pwa.ts` will unregister on next boot.  Dev users can also
  unregister via DevTools → Application → Service Workers →
  Unregister.
* **Drop the routes**: revert the four PWA-route additions in
  `main.py`.  Existing service workers will fail to fetch and
  silently expire after the cache TTL.
* **Drop Tauri spike**: `rm -rf desktop/tauri/`.  Zero impact on
  the live system; nothing else references that directory.

## Cycle C status

Sprint 12 closes the **final** sprint of Cycle C.  Every Sprint
deliverable in the original 12-sprint plan has shipped or has
explicit deferral notes — see `docs/cycle_c_complete.md` for the
end-to-end overview, the sprint matrix, and the Tauri-vs-PWA
decision rationale.
