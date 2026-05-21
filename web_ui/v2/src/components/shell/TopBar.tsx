import { type Component, type JSX, Show } from "solid-js";

export interface TopBarProps {
  title: string;
  subtitle?: string;
  /** Right-side action slot — typically a button or action group. */
  actions?: JSX.Element;
}

/**
 * Per-mode topbar.  Title + optional subtitle on the left, action
 * slot on the right.  An accent rule under the title pulls
 * ``--mode-accent`` so each mode has a subtle visual stamp.
 */
export const TopBar: Component<TopBarProps> = (props) => {
  return (
    <header class="flex h-14 shrink-0 items-center justify-between border-b border-border-subtle bg-bg-canvas px-5">
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span
            class="h-1.5 w-1.5 rounded-full"
            style={{ background: "var(--mode-accent)" }}
            aria-hidden="true"
          />
          <h1 class="truncate text-base font-semibold tracking-tight">
            {props.title}
          </h1>
        </div>
        <Show when={props.subtitle}>
          <p class="truncate text-xs text-text-subtle">{props.subtitle}</p>
        </Show>
      </div>
      <Show when={props.actions}>
        <div class="flex items-center gap-2">{props.actions}</div>
      </Show>
    </header>
  );
};
