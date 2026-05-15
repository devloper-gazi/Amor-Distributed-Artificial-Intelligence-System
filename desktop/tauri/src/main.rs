// Cycle C Sprint 12 Day 2 — Tauri 2.0 entrypoint (spike).
//
// Minimal `main()` that hands off to the Tauri builder.  Everything
// AMOR-specific lives in the Tauri config (tauri.conf.json).  We
// deliberately don't add custom IPC commands today — the spike's
// goal is to measure what an unmodified PWA wrapper costs.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .setup(|_app| Ok(()))
        .run(tauri::generate_context!())
        .expect("amor-desktop: tauri runtime failed to start");
}
