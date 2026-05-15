/**
 * Shared QueryClient + a tiny invalidate helper so module-scoped code
 * (e.g. Build/Research/Thinking ``start`` functions defined outside a
 * Solid component) can invalidate the Sessions sidebar without having
 * to call ``useQueryClient()`` from a component context.
 *
 * The QueryClient itself is created in ``main.tsx`` and assigned here
 * via ``setQueryClient(...)`` exactly once during boot.  Any code that
 * needs to invalidate calls ``invalidateSessionsList()``; if the boot
 * race lands before the QueryClient is set (impossible in practice
 * because ``main.tsx`` runs before any user click) we silently no-op.
 */

import type { QueryClient } from "@tanstack/solid-query";

let _client: QueryClient | null = null;

export function setQueryClient(client: QueryClient): void {
  _client = client;
}

export function getQueryClient(): QueryClient | null {
  return _client;
}

/** Invalidate the Sessions sidebar query so a freshly-created
 *  chat session appears immediately. */
export function invalidateSessionsList(): void {
  if (!_client) return;
  void _client.invalidateQueries({ queryKey: ["sessions", "list"] });
}
