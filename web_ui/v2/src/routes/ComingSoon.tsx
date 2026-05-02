import { type Component } from "solid-js";
import type { ModeMeta } from "../lib/types";
import { TopBar } from "../components/shell/TopBar";

interface ComingSoonProps {
  mode: ModeMeta;
}

/**
 * Stub for modes that haven't been re-implemented in v2 yet.
 * Renders the per-mode chrome + a clear CTA pointing back to the
 * legacy UI where the mode is still fully featured.
 */
export const ComingSoon: Component<ComingSoonProps> = (props) => {
  return (
    <div data-mode={props.mode.key} class="flex h-full flex-col">
      <TopBar title={props.mode.label} subtitle={props.mode.subtitle} />
      <div class="flex flex-1 items-center justify-center px-6 py-8">
        <div class="max-w-md rounded-lg border border-border-subtle bg-bg-elevated p-6 text-center">
          <span
            class="mx-auto block h-2 w-12 rounded-full"
            style={{ background: "var(--mode-accent)" }}
            aria-hidden="true"
          />
          <h2 class="mt-4 text-lg font-semibold tracking-tight">
            {props.mode.label} — coming to v2 soon
          </h2>
          <p class="mt-2 text-sm text-text-secondary">
            This mode is still hosted in the legacy UI while we
            re-build it for v2.  Open it there to keep working.
          </p>
          <a
            href="/legacy"
            class="mt-5 inline-flex h-9 items-center rounded-md bg-text-primary px-4 text-sm font-medium text-text-inverse hover:opacity-90"
          >
            Open legacy UI →
          </a>
        </div>
      </div>
    </div>
  );
};
