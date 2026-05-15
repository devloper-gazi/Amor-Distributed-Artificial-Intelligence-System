# AMOR desktop — Tauri 2.0 spike

> Cycle C Sprint 12 Day 2 — **scaffold only**.  Not built, not
> shipped.  Operators who want a native desktop AMOR run the steps
> below themselves; the PWA at `https://amor.local/` (or the
> compose-internal `http://localhost:8000/`) is the canonical
> delivery channel.

## What this directory contains

```
tauri/
├── Cargo.toml          # Rust crate manifest (release profile = size-opt)
├── tauri.conf.json     # window + bundle + CSP + i18n metadata
├── src/main.rs         # ~10-line entrypoint, no custom IPC
└── README.md           # this file
```

## Why a spike, not a release

Per the Cycle C plan:

> Decision post-Sprint-12: ship Tauri shell only if it adds value
> (file-system integration with bind-mounted host repo,
> auto-update).  PWA is default delivery; Tauri opt-in for power
> users.

The PWA already gives AMOR:

* Installable shell with offline shell-cache (Sprint 12 Day 1)
* Standalone window via `display: standalone` in
  `manifest.webmanifest`
* Native-ish keyboard shortcuts (⌘K palette, etc.)
* Push notifications when implemented

A Tauri shell adds value ONLY when AMOR needs:

* Direct host-filesystem access without the FastAPI sandbox round-trip
* OS-level update channel (Squirrel / WiX bundling)
* System-tray integration

None of those are required by today's user surface, so we measure
the cost first and decide if/when to ship.

## How to build the spike (operator-driven)

```powershell
# Prerequisites — run once.
rustup install stable
cargo install tauri-cli --version "^2.0"

# Build the AMOR PWA bundle.  Tauri's beforeBuildCommand runs this
# automatically; doing it ahead of time lets you debug the JS
# build separately.
cd web_ui\v2
npm install
npm run build

# Build the desktop bundle.
cd ..\..\desktop\tauri
cargo tauri build --target x86_64-pc-windows-msvc

# Outputs land in:
#   target\release\bundle\msi\AMOR_0.12.0_x64_en-US.msi
#   target\release\bundle\nsis\AMOR_0.12.0_x64-setup.exe
```

## Measurements to capture

Record these in `docs/sprint12_results.md`:

| Metric | Expected (pkgpulse 2026) | Tauri actual | Electron baseline |
|--------|--------------------------|--------------|-------------------|
| Installer size | 8 – 12 MB | _measure_ | 80 – 200 MB |
| Resident memory (idle) | 80 – 120 MB | _measure_ | 250 – 500 MB |
| First-paint after launch | < 1.5 s | _measure_ | 2 – 4 s |
| Build time (clean) | 60 – 180 s | _measure_ | 30 – 90 s |

`pkgpulse 2026` benchmarks favour Tauri ~10× on bundle size and
~3× on RAM; expect those ratios to hold within an order of
magnitude.  Build time is Tauri's known weakness — Cargo + WebView2
linking is heavier than Electron's "copy node_modules and ship".

## Caveats / known unknowns

* **WebView2 dep on Windows**: end-users on Windows 10 1803+ usually
  already have it.  Older builds need the bootstrapper installed
  out of band; Tauri's NSIS target can include it
  (`bundle.nsis.bootstrapInstaller = true`) — not enabled today
  pending a measurement of the size delta.
* **Auto-update**: not wired.  Tauri 2's update system requires
  signed manifests; Sprint 12's spike scope deliberately excludes
  the signing infrastructure.  Decision deferred.
* **Linux / macOS**: Tauri supports both, but Cycle C is
  Windows-first.  Producing AppImage / DMG / DEB is a follow-up
  cycle decision after Sprint 12's data.
* **Mobile**: Tauri 2 added iOS / Android targets.  Out of scope —
  Sprint 11 already shipped a responsive PWA that covers those
  surfaces without a dedicated bundle.

## Why this is not in the bundle-size CI gate

The Tauri spike doesn't ship binaries to users; the JS bundle gate
in `web_ui/v2/tools/check_bundle_size.mjs` covers what users
actually download.  Future work could add a Rust bundle gate if
Tauri ever ships.

## Rollback

This entire directory can be deleted with no impact on the live
system.  No build script references it; no docker-compose service
mounts it.  It's pure operator-side scaffolding.
