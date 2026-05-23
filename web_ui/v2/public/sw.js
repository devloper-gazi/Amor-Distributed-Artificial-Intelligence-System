/**
 * AMOR v2 — service worker.
 *
 * Cycle C Sprint 12 Day 1.  Hand-rolled rather than via vite-plugin-
 * pwa to keep the dependency surface small and the cache rules
 * grep-able.  ~80 LOC including comments.
 *
 * Caching strategy
 * ----------------
 *
 * * **Static shell** (``/``, ``/index.html``, hashed
 *   ``/static/v2/assets/*``, manifest, icons) — *cache-first*.
 *   The cache is keyed on a build hash baked into ``CACHE_VERSION``;
 *   bumping that value invalidates the whole cache on the next
 *   ``activate``.
 *
 * * **API + SSE** (``/api/*``, ``/v1/*``, ``/mcp/*``) — *network-only*.
 *   Streaming endpoints can't be cached meaningfully and ``/api`` is
 *   the source of truth for everything mutable; serving a stale
 *   response would mislead the user.  When offline these requests
 *   fail fast — the UI's existing error banners (``ConnectionBanner``)
 *   already handle the degraded state.
 *
 * * **Route navigations** (``index.html`` for any non-``/api`` path)
 *   — *cache-first with a network refresh in the background*.  This
 *   makes the app instantly available offline while keeping it
 *   up-to-date when the user comes back online.
 *
 * The 50-message-per-chat offline buffer the Cycle C plan calls for
 * is handled at the IndexedDB layer (future work) rather than in
 * the SW — chat-stream snapshots are already persisted via
 * ``localStorage["amor.chat.v1.<mode>.turns"]`` (Sprint 4 Day 1).
 */

/* eslint-disable no-restricted-globals */

const CACHE_VERSION = "amor-v1-sw-2026-05-04";
const STATIC_ALLOWLIST = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icon-192.svg",
  "/icon-512.svg",
];

self.addEventListener("install", (event) => {
  // Pre-cache the shell so a fresh install boots offline immediately.
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(STATIC_ALLOWLIST)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Drop any cache that doesn't match the current version.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  // Network-only paths — see strategy comment above.
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/v1/") ||
    url.pathname.startsWith("/mcp/")
  ) {
    return; // let the browser run the default fetch
  }

  // Page navigations — cache-first with background refresh so the
  // app shell pops up instantly + an updated copy lands for next
  // time.  We deliberately serve ``index.html`` for any unknown
  // route because the SolidJS router resolves the path client-side.
  if (request.mode === "navigate") {
    event.respondWith(handleNavigate(request));
    return;
  }

  // Static assets / fonts / icons — straight cache-first.
  event.respondWith(handleStaticAsset(request));
});

async function handleNavigate(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached =
    (await cache.match(request)) ?? (await cache.match("/index.html"));
  // Kick off a network refresh; don't await it from the user's
  // perspective so navigation stays instant.
  const refresh = fetch(request)
    .then(async (response) => {
      if (response && response.ok) {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => null);
  if (cached) {
    refresh.catch(() => undefined);
    return cached;
  }
  const network = await refresh;
  return network ?? cache.match("/index.html") ?? Response.error();
}

async function handleStaticAsset(request) {
  const cache = await caches.open(CACHE_VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  try {
    const response = await fetch(request);
    if (response && response.ok && new URL(request.url).origin === self.location.origin) {
      // Only cache same-origin successes — third-party fonts /
      // CDNs don't need our cache pollution.
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return cache.match("/index.html") ?? Response.error();
  }
}
