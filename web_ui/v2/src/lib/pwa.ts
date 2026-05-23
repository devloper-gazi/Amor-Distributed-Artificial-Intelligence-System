/**
 * Cycle C Sprint 12 Day 1 — service-worker registration helper.
 *
 * Called once at boot from ``main.tsx``.  We deliberately keep the
 * registration code in its own module so:
 *
 * * It can be ``import``-ed lazily on production-only paths in
 *   the future (the SW is harmful for HMR-heavy dev sessions).
 * * Its environment guards (``isProductionLike()`` /
 *   ``serviceWorkerSupported()``) are unit-testable in node env
 *   without touching the DOM.
 *
 * Three opt-out / opt-in env handles:
 *
 *   ``import.meta.env.DEV``       — true in ``vite dev``; we skip
 *                                    registration so a stale cached
 *                                    bundle doesn't shadow HMR.
 *   ``localStorage["amor.pwa"]``  — operator override; ``"off"``
 *                                    forces unregister (handy when
 *                                    debugging caching weirdness).
 *   ``navigator.serviceWorker``   — feature detect; missing on
 *                                    older Safari / privacy modes.
 */

const SW_PATH = "/sw.js";
const SW_SCOPE = "/";
const PWA_LS_KEY = "amor.pwa";


export function serviceWorkerSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator
  );
}


/** True when we're in a build/CI bundle — i.e. NOT ``vite dev``. */
export function isProductionLike(): boolean {
  // ``import.meta.env.PROD`` is the canonical Vite signal.  Fall
  // back to NODE_ENV if Vite isn't injecting it (old bundlers).
  if (typeof import.meta !== "undefined" && import.meta.env) {
    return Boolean(import.meta.env.PROD);
  }
  return (
    typeof process !== "undefined" &&
    process.env?.NODE_ENV === "production"
  );
}


export function pwaForceDisabled(): boolean {
  try {
    return localStorage.getItem(PWA_LS_KEY) === "off";
  } catch {
    return false;
  }
}


/**
 * Register the service worker if and only if we're production AND
 * the operator hasn't disabled it.  Returns the registration so
 * tests can assert on it; production callers ignore the result.
 *
 * Errors are swallowed — a SW registration failure must never
 * crash the main bundle.
 */
export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!serviceWorkerSupported()) return null;
  if (!isProductionLike()) return null;
  if (pwaForceDisabled()) {
    // Operator opted out — clean up any prior registration so the
    // old SW doesn't keep serving stale assets after the toggle.
    void unregisterServiceWorker();
    return null;
  }
  try {
    const reg = await navigator.serviceWorker.register(SW_PATH, {
      scope: SW_SCOPE,
      type: "classic",
    });
    // Listen for an updated SW landing — ``controllerchange`` fires
    // once the new worker takes over so we can prompt the user
    // (handled at the UI layer; today we just log).
    if (reg.waiting) {
      // Force activate immediately — the SW already calls
      // ``self.skipWaiting()`` on install but a manual prompt
      // covers the case where the old worker was still controlling.
      reg.waiting.postMessage({ type: "SKIP_WAITING" });
    }
    return reg;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("amor.pwa: SW registration failed", err);
    return null;
  }
}


export async function unregisterServiceWorker(): Promise<boolean> {
  if (!serviceWorkerSupported()) return false;
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    let dropped = 0;
    for (const r of regs) {
      const ok = await r.unregister();
      if (ok) dropped += 1;
    }
    return dropped > 0;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("amor.pwa: unregister failed", err);
    return false;
  }
}
