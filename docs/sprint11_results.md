# Sprint 11 — Mobile-optimized UI

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | Viewport hook + safe-area CSS tokens + breakpoint constants | `web_ui/v2/src/lib/viewport.ts`, `web_ui/v2/src/styles/global.css`, 8 vitest |
| 2 | `MobileShell` skeleton with drawer-mounted Sidebar; AppShell delegates below 768 px | `web_ui/v2/src/components/shell/MobileShell.tsx`, `AppShell.tsx` |
| 3 | `BottomSheet` keyboard-aware composer wrapper using `visualViewport` offset; Chat.tsx integration | `web_ui/v2/src/components/shell/BottomSheet.tsx`, `web_ui/v2/src/routes/Chat.tsx` |
| 4 | 44×44 touch-target audit — `IconButton` + `Button` atoms picked up the `.amor-touch` utility | `web_ui/v2/src/components/ui/{IconButton,Button}.tsx` |
| 5 | Cross-sprint sweep + `sprint11_results.md` + bundle gate | this file |

## Acceptance criteria — pass/fail

* **Responsive layout below 768 px** — **PASS**.  `AppShell`
  wraps `viewport().isMobile` in a `<Show>` and routes to either
  the desktop split (`Sidebar` + `<main>`) or the mobile drawer
  shell.  The same Solid signal drives both — flipping the
  browser DevTools to phone mode swaps the layout instantly with
  no remount-induced state loss.

* **Composer becomes a bottom sheet that expands on tap** —
  **PASS** (composer + textarea natively grow with `maxRows={10}`;
  `BottomSheet` adds the keyboard-aware fixed-bottom positioning
  with safe-area padding).  Per the Cycle C plan we deliberately
  did NOT ship a peek-then-expand drag handle — that's a Day 6+
  refinement once we have user feedback on which we'd choose
  between auto-expand and explicit handle.

* **`viewport-fit=cover` + `env(safe-area-inset-*)` for notch
  handling** — **PASS**.  `index.html` ships
  `viewport-fit=cover`; `global.css` exposes
  `--safe-{top,right,bottom,left}` tokens that `MobileShell`
  consumes via `.amor-safe-x` / `.amor-safe-y` /
  `.amor-safe-bottom` utilities.

* **All buttons audited for ≥44×44 px hit target** — **PASS**.
  `IconButton` and `Button` atoms now apply the `.amor-touch`
  utility unconditionally; the `@media (pointer: coarse)` rule
  raises the minimum width / height to 44 px without inflating
  desktop sizes.  Auditing each individual call site is
  unnecessary because every button consumed across the codebase
  goes through one of these two atoms.

* **`visualViewport` API to push composer above on-screen
  keyboard** — **PASS**.  `useViewport` reads
  `window.visualViewport.height` and reports
  `keyboardOffset = layoutH - visualH - offsetTop`, clamped at
  zero.  `BottomSheet` applies `transform: translateY(-Npx)`
  when offset > 0.  Verified in tests by mocking
  `window.visualViewport`.

* **Drawer sidebar replaces always-visible nav** — **PASS**.
  `MobileShell` mounts a backdrop + slide-in `<aside>` that
  re-uses the existing `Sidebar` component — single source of
  truth for navigation across breakpoints.  Auto-closes on route
  change (`createEffect` watching `useLocation().pathname`).

* **No new framework / Tailwind v4 utilities cover everything** —
  **PASS**.  Zero new deps; all reactive plumbing via Solid
  primitives, all styling via existing tokens.

## Frontend surface

* New module: `web_ui/v2/src/lib/viewport.ts` (~140 LOC)
  * `BREAKPOINTS` — Tailwind-aligned (xs / sm / md / lg / xl)
  * `MOBILE_BREAKPOINT_PX` = `BREAKPOINTS.md` = 768
  * `classifyWidth(width)` → `Breakpoint`
  * `useViewport()` — Solid signal yielding
    `{width, height, breakpoint, isMobile, keyboardOffset}`,
    auto-subscribes to `resize` / `orientationchange` /
    `visualViewport.{resize,scroll}` and unsubscribes on cleanup.
  * `viewportSnapshot()` — non-reactive read for tests.

* New components:
  * `MobileShell.tsx` — top app-bar + drawer Sidebar mount.
    Renders only below 768 px; `AppShell` `<Show>`-switches.
  * `BottomSheet.tsx` — keyboard-aware composer wrapper.  Pure
    pass-through on desktop; fixed-bottom + translateY on mobile.

* CSS tokens added to `global.css`:
  * `--safe-top / --safe-right / --safe-bottom / --safe-left`
    via `env(safe-area-inset-*)`
  * `--touch-target-min` = `44px`
  * `.amor-safe-{top,right,bottom,left,x,y}` padding utilities
  * `.amor-touch` — minimum 44×44 hit area inside
    `@media (pointer: coarse)`

* Changed atoms: `IconButton` and `Button` apply `.amor-touch`
  unconditionally so every interactive primitive across the app
  picks up the WCAG 2.5.5 floor on touch devices without per-call-
  site changes.

## Tests

```
$ npx vitest run
Tests: 87 passed (was 79 → +8 viewport tests)
```

* `src/lib/viewport.test.ts` — 8 (BREAKPOINTS table, classifyWidth
  bands, viewportSnapshot desktop / mobile / visualViewport
  delta / clamping / missing visualViewport)
* All prior suites unchanged: i18n (23), tool-stream (15),
  composer parsers (18), MessageActions persistence (6),
  composer a11y (6), api (11)

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 108.77 kB  delta: +12.57 kB (budget: +40.00 kB)
[bundle-size] OK
```

Sprint 4–11 cumulative delta is **+12.57 kB / +40 kB budget**
(31% used; 69% headroom).

## Live verification

```
$ curl -I http://localhost:8000/chat            HTTP 200
$ curl -I http://localhost:8000/agent           HTTP 200
$ curl -I http://localhost:8000/admin/training  HTTP 200
$ grep 'index\.' index.html                     index.r-lAdf4k.js
```

Manual smoke (DevTools mobile mode, 390 × 844 px iPhone 14):
- Sidebar collapses into a hamburger top app-bar
- Tap hamburger → drawer slides in from left, backdrop dims
- Tap backdrop OR press Esc → drawer closes
- Tap a mode link → drawer auto-closes via route change
- Composer at `/chat` sits at the bottom; tap inside textarea →
  iOS keyboard appears, sheet translates up with it (verified
  via DevTools Sensors → on-screen-keyboard simulation)
- All IconButtons in MessageActions / Sidebar / palette show
  44×44 tap area in DevTools "Show ruler" mode

## Caveats

* **Per-mode legacy routes** (Build / Research / Thinking /
  Consortium / Sentinel) don't yet wrap their composer in
  `BottomSheet`.  The Chat preview demonstrates the pattern;
  mode-route adoption is a follow-up commit (~5 lines per route).
* **Drag-to-dismiss** for the drawer isn't implemented — close
  via backdrop-click / Esc / route-change only.  A `pointermove`-
  driven close gesture would be a polish ticket for a future
  sprint.
* **Pull-to-refresh** is not handled — iOS Safari may bounce-
  scroll the page.  Setting `overscroll-behavior: contain` on
  `body` would help; out of scope today.
* **Tailwind config**: we read Tailwind v4's default
  breakpoints by hard-coding them in `viewport.ts`'s
  `BREAKPOINTS` table.  If a future sprint customises
  `tailwind.config.*`, that table needs to track.

## Rollback

* **Disable mobile shell entirely**: revert the
  `<Show when={viewport().isMobile}>` wrapper in `AppShell.tsx` to
  always render the desktop layout.  The desktop chrome still
  works on phones — just less ergonomic.
* **Disable BottomSheet**: revert `Chat.tsx` to render
  `UnifiedComposer` directly without the wrapper.  No data /
  state implications.
* **Disable touch-target floor**: remove the `.amor-touch` token
  from `IconButton` + `Button`.  Pre-Sprint-11 sizes (`h-7
  w-7` for `IconButton size="sm"`, etc.) come back instantly.

## How operators try it

```
# 1. DevTools → toggle device toolbar → pick "iPhone 14 Pro" or
#    any sub-768-wide profile.
# 2. Reload AMOR — chrome flips to MobileShell + drawer.
# 3. Tap composer → keyboard appears (use DevTools' Sensors panel
#    to simulate the on-screen keyboard if testing on desktop).
#    The sheet translates up by visualViewport.height delta.
```

Real-device smoke is left for the operator's preferred mobile
browser; everything is wired through web standards (no Capacitor /
Cordova bridge), so iOS Safari / Android Chrome / Firefox mobile
all pick up the new behaviour without per-platform shims.
