import { type Component } from "solid-js";

/**
 * Placeholder root view.  Replaced in PR-4 (Chat-mode parity rewrite)
 * with the real AppShell + Sidebar + CommandPalette.  For PR-1 the
 * goal is to verify the build pipeline + Tailwind v4 + theme tokens
 * end-to-end on a single page.
 */
export const App: Component = () => {
  return (
    <main
      data-mode="system"
      class="flex min-h-screen flex-col items-center justify-center bg-bg-primary text-text-primary"
    >
      <div class="max-w-md text-center">
        <div class="text-3xl font-semibold tracking-tight">AMOR v2</div>
        <p class="mt-2 text-sm text-text-secondary">
          Local-first distributed AI desktop &middot; web UI redesign
        </p>
        <div class="mt-6 flex flex-col items-center gap-3 text-sm">
          <a
            href="/showcase"
            class="rounded-md border border-border-default bg-bg-elevated px-4 py-2 hover:bg-bg-hover"
          >
            Open component showcase &rarr;
          </a>
          <span class="text-xs text-text-tertiary">
            PR-1 scaffold &middot; build pipeline + theme tokens only
          </span>
        </div>
      </div>
    </main>
  );
};
